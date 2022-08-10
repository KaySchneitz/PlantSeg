import math
from concurrent.futures import Future
from enum import Enum
from functools import partial
from typing import Tuple, Union

import numpy as np
from magicgui import magicgui
from napari.layers import Image, Labels, Shapes, Layer
from napari.types import LayerDataTuple

from plantseg.dataprocessing.functional import image_gaussian_smoothing, image_rescale
from plantseg.dataprocessing.functional.dataprocessing import compute_scaling_factor, compute_scaling_voxelsize
from plantseg.gui import list_models, get_model_resolution
from plantseg.napari.widget.utils import start_threading_process, build_nice_name


def _generic_preprocessing(image_data, sigma, gaussian_smoothing, rescale, rescaling_factors):
    if gaussian_smoothing:
        image_data = image_gaussian_smoothing(image=image_data, sigma=sigma)
    if rescale:
        image_data = image_rescale(image=image_data, factor=rescaling_factors, order=1)

    return image_data


@magicgui(call_button='Run Gaussian Smoothing',
          sigma={"widget_type": "FloatSlider", "max": 5., 'min': 0.1})
def widget_gaussian_smoothing(image: Image,
                              sigma: float = 1.,
                              ) -> Future[LayerDataTuple]:
    out_name = build_nice_name(image.name, 'GaussianSmoothing')
    inputs_kwarg = {'image': image.data}
    inputs_names = (image.name,)
    layer_kwargs = {'name': out_name, 'scale': image.scale}
    layer_type = 'image'
    func = partial(image_gaussian_smoothing, sigma=sigma)

    return start_threading_process(func,
                                   func_kwargs=inputs_kwarg,
                                   out_name=out_name,
                                   input_keys=inputs_names,
                                   layer_kwarg=layer_kwargs,
                                   layer_type=layer_type,
                                   )


@magicgui(call_button='Run Image Rescaling',
          type_of_refactor={'widget_type': 'RadioButtons',
                            'orientation': 'vertical',
                            'choices': ['Rescaling factor',
                                        'Voxel size (um)',
                                        'Same as Reference Layer',
                                        'Same as Reference Model']},
          reference_model={"choices": list_models()})
def widget_rescaling(image: Layer,
                     type_of_refactor: str = 'Rescaling factor',
                     rescaling_factor: Tuple[float, float, float] = (1., 1., 1.),
                     out_voxel_size: Tuple[float, float, float] = (1., 1., 1.),
                     reference_layer: Union[None, Layer] = None,
                     reference_model: str = list_models()[0],
                     order: int = 1,
                     ) -> Future[LayerDataTuple]:

    if isinstance(image, Image):
        pass

    elif isinstance(image, Labels):
        order = 0

    else:
        raise ValueError(f'{type(image)} cannot be rescaled, please use Image layers or Labels layers')

    current_resolution = image.scale
    if type_of_refactor == 'Voxel size (um)':
        rescaling_factor = compute_scaling_factor(current_resolution, out_voxel_size)

    elif type_of_refactor == 'Same as Reference Layer':
        out_voxel_size = reference_layer.scale
        rescaling_factor = compute_scaling_factor(current_resolution, reference_layer.scale)

    elif type_of_refactor == 'Same as Reference Model':
        out_voxel_size = get_model_resolution(reference_model)
        print(out_voxel_size)
        rescaling_factor = compute_scaling_factor(current_resolution, out_voxel_size)

    else:
        out_voxel_size = compute_scaling_voxelsize(current_resolution, scaling_factor=rescaling_factor)

    out_name = build_nice_name(image.name, 'Rescaled')
    inputs_kwarg = {'image': image.data}
    inputs_names = (image.name, )
    layer_kwargs = {'name': out_name,
                    'scale': out_voxel_size,
                    'metadata': {'original_voxel_size': current_resolution}}
    layer_type = 'image'
    func = partial(image_rescale, factor=rescaling_factor, order=order)

    return start_threading_process(func,
                                   func_kwargs=inputs_kwarg,
                                   out_name=out_name,
                                   input_keys=inputs_names,
                                   layer_kwarg=layer_kwargs,
                                   layer_type=layer_type,
                                   )


def _cropping(data, crop_slices):
    return data[crop_slices]


@magicgui(call_button='Run Cropping', )
def widget_cropping(image: Layer,
                    crop_roi: Union[Shapes, None] = None,
                    crop_z: int = 1,
                    ) -> Future[LayerDataTuple]:
    assert len(crop_roi.shape_type) == 1, "Only one rectangle should be used for cropping"
    assert crop_roi.shape_type[0] == 'rectangle', "Only a rectangle shape should be used for cropping"

    out_name = build_nice_name(image.name, 'cropped')
    inputs_names = (image.name,)
    layer_kwargs = {'name': out_name, 'scale': image.scale}
    layer_type = 'image'

    rectangle = crop_roi.data[0].astype('int64')
    crop_slices = [slice(rectangle[0, 0] - crop_z // 2, rectangle[0, 0] + math.ceil(crop_z / 2)),
                   slice(rectangle[0, 1], rectangle[2, 1]),
                   slice(rectangle[0, 2], rectangle[2, 2])]

    func = partial(_cropping, crop_slices=crop_slices)
    return start_threading_process(func,
                                   func_kwargs={'data': image.data},
                                   out_name=out_name,
                                   input_keys=inputs_names,
                                   layer_kwarg=layer_kwargs,
                                   layer_type=layer_type,
                                   skip_dag=True,
                                   )


class Operation(Enum):
    add = np.add
    subtract = np.subtract
    maximum = np.max
    minimum = np.min


def _two_layers_operation(data1, data2, operation, weights: float = 0.5):
    if operation == 'Mean':
        return weights * data1 + (1. - weights) * data2
    elif operation == 'Maximum':
        return np.maximum(data1, data2)
    else:
        return np.minimum(data1, data2)


@magicgui(call_button='Run Merge Layers',
          operation={'widget_type': 'RadioButtons',
                     'orientation': 'horizontal',
                     'choices': ['Mean',
                                 'Maximum',
                                 'Minimum']},
          weights={"widget_type": "FloatSlider", "max": 1., 'min': 0.},
          )
def widget_add_layers(image1: Image,
                      image2: Image,
                      operation: str,
                      weights: float = 0.5,
                      ) -> Future[LayerDataTuple]:

    out_name = build_nice_name(f'{image1.name}-{image2.name}', operation)
    inputs_names = (image1.name, image2.name)
    layer_kwargs = {'name': out_name, 'scale': image1.scale}
    layer_type = 'image'

    func = partial(_two_layers_operation, weights=weights, operation=operation)
    assert image1.data.shape == image2.data.shape
    return start_threading_process(func,
                                   func_kwargs={'data1': image1.data, 'data2': image2.data},
                                   out_name=out_name,
                                   input_keys=inputs_names,
                                   layer_kwarg=layer_kwargs,
                                   layer_type=layer_type,
                                   )
