import copy
from typing import Optional

import numba
import numpy as np
import tqdm
from skimage.filters import gaussian
from skimage.segmentation import watershed


def get_bbox(mask: np.array, pixel_toll: int = 0) -> tuple[tuple, int, int, int]:
    """
    returns the bounding box around a binary mask
    """
    max_shape = mask.shape
    coords = np.nonzero(mask)
    z_min, z_max = max(coords[0].min() - pixel_toll, 0), min(coords[0].max() + pixel_toll, max_shape[0])
    z_max = z_max if z_max - z_min > 0 else 1
    x_min, x_max = max(coords[1].min() - pixel_toll, 0), min(coords[1].max() + pixel_toll, max_shape[1])
    y_min, y_max = max(coords[2].min() - pixel_toll, 0), min(coords[2].max() + pixel_toll, max_shape[2])
    return (slice(z_min, z_max), slice(x_min, x_max), slice(y_min, y_max)), z_min, x_min, y_min


def get_quantile_mask(counts: np.array, quantile: tuple[float, float] = (0.2, 0.99)) -> np.array:
    """
    filters counts by quantiles
    """
    mask_low = np.quantile(counts, quantile[0]) < counts
    mask_up = np.quantile(counts, quantile[1]) > counts
    mask = np.logical_and(mask_low, mask_up)
    return mask


@numba.njit(parallel=True)
def numba_find_overlaps(cell_seg: np.array, n_seg: np.array) -> tuple[np.array, np.array, np.array]:
    """
    given a cell_seg: cell segmentation and a n_seg: nuclei segmentation
    returns: cell_sizes, nuclei_sizes, pixel overlap
    """
    shape_x, shape_y, shape_z = cell_seg.shape

    cell_counts = np.zeros(cell_seg.max() + 1).astype(np.uint16)
    n_counts = np.zeros(n_seg.max() + 1).astype(np.uint16)
    overlap_counts = np.zeros((cell_seg.max() + 1, n_seg.max() + 1)).astype(np.uint16)

    for x in numba.prange(shape_x):
        for y in range(shape_y):
            for z in range(shape_z):
                seg_id = cell_seg[x, y, z]
                if seg_id > 0:
                    cell_counts[seg_id] += 1

                n_id = n_seg[x, y, z]
                if n_id > 0:
                    n_counts[n_id] += 1

                if n_id > 0 and seg_id > 0:
                    overlap_counts[seg_id, n_id] += 1

    return cell_counts, n_counts, overlap_counts


def find_potential_under_seg(nuclei_counts: np.array,
                             cell_counts: np.array,
                             intersection_counts: np.array,
                             threshold: float = 0.9,
                             quantiles_clip: tuple[float, float] = (0.2, 0.99)) -> dict:
    """
    returns for each cell idx a dict containing the overlap profile between said cell and the nuclei
    """
    nuclei_counts_mask = get_quantile_mask(nuclei_counts, quantiles_clip)
    cell_assigment = {}
    for idx in range(cell_counts.shape[0]):
        n_idx = np.nonzero(intersection_counts[idx])[0]
        r_intersection = [intersection_counts[idx, _n_idx] / nuclei_counts[_n_idx] for _n_idx in n_idx]

        under_seg_n_idx = []
        for _n_idx, r_i in zip(n_idx, r_intersection):
            if r_i > threshold and nuclei_counts_mask[_n_idx]:
                under_seg_n_idx.append(_n_idx)

        is_under_seg = True if len(under_seg_n_idx) > 1 else False

        if is_under_seg:
            cell_assigment[idx] = {'n_idx': n_idx,
                                   'under_seg_idx': under_seg_n_idx,
                                   'is_under_seg': is_under_seg,
                                   'r_intersection': r_intersection}
    return cell_assigment


def find_potential_over_seg(nuclei_counts: np.array,
                            intersection_counts: np.array,
                            threshold: float = 0.3) -> dict:
    """
    returns for each nucleus idx a dict containing the overlap profile between said nucleus and the segmentation
    """

    nuclei_assigment = {}
    for idx in range(nuclei_counts.shape[0]):
        c_idx = np.nonzero(intersection_counts[:, idx])[0]

        r_intersection = [intersection_counts[_c_idx, idx] / nuclei_counts[idx] for _c_idx in c_idx]

        over_seg_c_idx = []
        for _c_idx, r_i in zip(c_idx, r_intersection):
            if r_i > threshold:
                over_seg_c_idx.append(_c_idx)

        is_over_seg = True if len(over_seg_c_idx) > 1 else False
        if is_over_seg:
            nuclei_assigment[idx] = {'c_idx': c_idx,
                                     'over_seg_idx': over_seg_c_idx,
                                     'is_over_seg': is_over_seg,
                                     'r_intersection': r_intersection}
    return nuclei_assigment


def split_from_seeds(segmentation: np.array,
                     boundary_pmap: np.array,
                     seeds: np.array,
                     all_idx: list[int]) -> np.array:
    """
    Split a segmentation using seeds watershed
    """
    # find seeds location ad label value
    c_segmentation = copy.deepcopy(segmentation)

    # create bbox from mask
    mask = np.logical_or.reduce([c_segmentation == label_idx for label_idx in all_idx])
    bbox, z_min, x_min, y_min = get_bbox(mask)

    _boundary_pmap = boundary_pmap[bbox]
    _seeds = seeds[bbox]
    _mask = np.logical_or.reduce([c_segmentation[bbox] == label_idx for label_idx in all_idx])

    # tobe refactored watershed segmentation
    _boundary_pmap = gaussian(_boundary_pmap / _boundary_pmap.max(), 2.0)
    local_seg = watershed(_boundary_pmap, markers=_seeds, compactness=0.001)
    max_id = c_segmentation.max()

    # copy unique labels in the source data
    local_seg += max_id + 1
    c_segmentation[bbox][_mask] = local_seg[_mask].ravel()
    return c_segmentation


def fix_under_segmentation(segmentation: np.array,
                           nuclei_segmentation: np.array,
                           boundary_pmap: np.array,
                           cell_assignments: dict,
                           cell_idx: Optional[list[int]] = None) -> np.array:
    """
    this function attempts to fix cell under segmentation in the cells by splitting cells with multiple nuclei
    """
    _segmentation = copy.deepcopy(segmentation)
    print(" -fixing under segmentation")
    for c_idx, value in tqdm.tqdm(cell_assignments.items()):

        if cell_idx is None or c_idx in cell_idx:
            _nuclei_seeds = np.zeros_like(_segmentation)
            for i, n_idx in enumerate(value['under_seg_idx']):
                _nuclei_seeds[n_idx == nuclei_segmentation] = i + 1

            _segmentation = split_from_seeds(_segmentation, boundary_pmap, _nuclei_seeds, all_idx=[c_idx])
    return _segmentation


def fix_over_segmentation(segmentation: np.array,
                          nuclei_assignments: dict,
                          nuclei_idx: Optional[list[int]] = None) -> np.array:
    """
    this function attempts to fix cell over segmentation by merging cells that splits in two a nucleus
    """
    _segmentation = copy.deepcopy(segmentation)
    print(" -fixing over segmentation")
    for n_idx, value in tqdm.tqdm(nuclei_assignments.items()):

        if nuclei_idx is None or n_idx in nuclei_idx:
            new_value = value['over_seg_idx'][0]
            for i, c_idx in enumerate(value['over_seg_idx']):
                _segmentation[segmentation == c_idx] = new_value
    return _segmentation


def fix_over_under_segmentation_from_nuclei(cell_seg: np.array,
                                            nuclei_seg: np.array,
                                            threshold_merge: float = 0.33,
                                            threshold_split: float = 0.66,
                                            quantiles_nuclei: tuple[float, float] = (0.3, 0.99),
                                            boundary: Optional[np.array] = None) -> np.array:
    """
    This function attempts to fix cell under and over segmentation, given a trusted nuclei segmentation of the
    same image.
    - To fix cell under segmentation, it will try to splitting cells with multiple nuclei
    - To fix cell over segmentation, it will try to merge cells that splits in two a nucleus

    Args:
        cell_seg (np.array): numpy array containing the cell segmentation
        nuclei_seg (np.array): numpy array containing the nuclei segmentation
        threshold_merge (float, optional): percentage of the nucleus overlapping each cell segment.
            If the overlap is smaller than the defined threshold, the script will not merge the two cells.
            Defaults to 0.33.
        threshold_split (float, optional): percentage of the nucleus overlapping each cell segment.
            If the overlap is smaller than the defined threshold, the script will not split the two cells.
            Defaults to 0.66.
        quantiles_nuclei (tuple[float, float], optional): Remove nuclei too small or too large according to
            their quantiles. Defaults to (0.3, 0.99).
        boundary (Optional[np.array], optional): Optional numpy array containing the boundary signal or,
            better, a boundary pmap. Defaults to None.
    Returns:
        np.array: The new cell segmentation
    """

    # measure the overlap between cell and nuclei 1st time
    cell_counts, nuclei_counts, cell_nuclei_counts = numba_find_overlaps(cell_seg, nuclei_seg)
    nuclei_assignments = find_potential_over_seg(nuclei_counts,
                                                 cell_nuclei_counts,
                                                 threshold=threshold_merge)

    _cell_seg = fix_over_segmentation(cell_seg, nuclei_assignments, nuclei_idx=None)

    # measure the overlap between cell and nuclei 2nd time after the merges
    cell_counts, nuclei_counts, cell_nuclei_counts = numba_find_overlaps(_cell_seg, nuclei_seg)
    cell_assignments = find_potential_under_seg(nuclei_counts,
                                                cell_counts,
                                                cell_nuclei_counts,
                                                threshold=threshold_split,
                                                quantiles_clip=quantiles_nuclei)

    boundary_pmap = np.ones_like(cell_seg) if boundary is None else boundary
    _cell_seg = fix_under_segmentation(_cell_seg,
                                       nuclei_seg,
                                       boundary_pmap,
                                       cell_assignments,
                                       cell_idx=None)
    return _cell_seg
