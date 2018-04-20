"""Function to manage skymodels.

"""

import logging

from arl.data.skymodel import SkyModel
from arl.image.operations import copy_image
from arl.skycomponent.base import copy_skycomponent
from arl.imaging.imaging_context import predict_function
from arl.imaging import predict_skycomponent_visibility
from arl.visibility.visibility_fitting import fit_visibility
from arl.image.solvers import solve_image

log = logging.getLogger(__name__)


def copy_skymodel(sm):
    """ Copy a sky model
    
    """
    return SkyModel(components = [copy_skycomponent(comp) for comp in sm.components],
                    images=[copy_image(im) for im in sm.images])


def predict_skymodel_visibility(vis, sm, **kwargs):
    """ Predict the visibility for a sky model.
    
    The skymodel is a collection of skycomponents and images
    
    :param vis:
    :param sm:
    :param kwargs:
    :return:
    """
    if sm.components is not None:
        vis = predict_skycomponent_visibility(vis, sm.components)
    if sm.images is not None:
        for im in sm.images:
            vis = predict_function(vis, im, **kwargs)
        
    return vis

def solve_skymodel(vis, skymodel, gain=0.1, **kwargs):
    """Fit a single skymodel to a visibility
    
    :param evis: Expected vis for this ssm
    :param calskymodel: scm element being fit i.e. (skymodel, gaintable) tuple
    :param gain: Gain in step
    :param method: 'fit' or 'sum'
    :param kwargs:
    :return: skycomponent
    """
    new_comps = list()
    for comp in skymodel.components:
        new_comp = copy_skycomponent(comp)
        new_comp, _ = fit_visibility(vis, new_comp)
        new_comp.flux = gain * new_comp.flux + (1.0 - gain) * comp.flux
        new_comps.append(new_comp)

    new_images = list()
    for im in skymodel.images:
        new_image = copy_image(im)
        new_image = solve_image(vis, new_image, **kwargs)
        new_images.append(new_image)
        
    return SkyModel(components=new_comps, images=new_images)

