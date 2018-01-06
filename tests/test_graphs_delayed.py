""" Unit tests for pipelines expressed via dask.delayed


"""

import os
import unittest

import numpy
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.wcs.utils import pixel_to_skycoord
from dask import delayed

from arl.calibration.operations import apply_gaintable, create_gaintable_from_blockvisibility
from arl.data.polarisation import PolarisationFrame
from arl.graphs.dask_init import get_dask_Client
from arl.graphs.delayed import create_invert_facet_graph, create_predict_facet_graph, \
    create_zero_vis_graph_list, create_subtract_vis_graph_list, create_deconvolve_channel_graph, \
    create_predict_graph, create_invert_graph, create_residual_graph,\
    create_residual_facet_graph, create_selfcal_graph_list, create_deconvolve_graph
from arl.graphs.vis import simple_vis
from arl.image.operations import qa_image, export_image_to_fits, copy_image, create_empty_image_like
from arl.imaging import create_image_from_visibility, predict_skycomponent_blockvisibility, \
    predict_skycomponent_visibility, invert_wstack_single, predict_wstack_single
from arl.skycomponent.operations import create_skycomponent, insert_skycomponent
from arl.util.testing_support import create_named_configuration
from arl.util.testing_support import simulate_gaintable
from arl.visibility.base import create_visibility, create_blockvisibility
from arl.visibility.operations import qa_visibility


class TestDaskGraphs(unittest.TestCase):
    def setUp(self):
        
        self.compute = True
        # Use the distributed scheduler
        
        self.results_dir = './test_results'
        os.makedirs(self.results_dir, exist_ok=True)
        
        self.npixel = 256
        self.facets = 4
        
        self.vis_graph_list = self.setupVis(add_errors=False)
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2])
    
    def setupVis(self, add_errors=False, freqwin=7, block=False):
        self.freqwin = freqwin
        vis_graph_list = list()
        self.ntimes = 5
        self.times = numpy.linspace(-3.0, +3.0, self.ntimes) * numpy.pi / 12.0
        self.frequency = numpy.linspace(0.8e8, 1.2e8, self.freqwin)
        
        for freq in self.frequency:
            vis_graph_list.append(delayed(self.ingest_visibility)(freq, times=self.times,
                                                                  add_errors=add_errors,
                                                                  block=block))
        
        self.nvis = len(vis_graph_list)
        self.wstep = 10.0
        self.vis_slices = 2 * int(numpy.ceil(numpy.max(numpy.abs(vis_graph_list[0].compute().w)) / self.wstep)) + 1
        return vis_graph_list
    
    def ingest_visibility(self, freq=1e8, chan_width=1e6, times=None, reffrequency=None,
                          add_errors=False, block=False):
        if times is None:
            times = (numpy.pi / 12.0) * numpy.linspace(-3.0, 3.0, 5)
        
        if reffrequency is None:
            reffrequency = [1e8]
        lowcore = create_named_configuration('LOWBD2-CORE')
        frequency = numpy.array([freq])
        channel_bandwidth = numpy.array([chan_width])
        
        phasecentre = SkyCoord(ra=+180.0 * u.deg, dec=-60.0 * u.deg, frame='icrs', equinox='J2000')
        if block:
            vt = create_blockvisibility(lowcore, times, frequency, channel_bandwidth=channel_bandwidth,
                                        weight=1.0, phasecentre=phasecentre,
                                        polarisation_frame=PolarisationFrame("stokesI"))
        else:
            vt = create_visibility(lowcore, times, frequency, channel_bandwidth=channel_bandwidth,
                                   weight=1.0, phasecentre=phasecentre,
                                   polarisation_frame=PolarisationFrame("stokesI"))
        cellsize = 0.001
        model = create_image_from_visibility(vt, npixel=self.npixel, cellsize=cellsize, npol=1,
                                             frequency=reffrequency, phasecentre=phasecentre,
                                             polarisation_frame=PolarisationFrame("stokesI"))
        flux = numpy.array([[100.0]])
        facets = 4
        
        rpix = model.wcs.wcs.crpix - 1.0
        spacing_pixels = self.npixel // facets
        centers = [-1.5, -0.5, 0.5, 1.5]
        comps = list()
        for iy in centers:
            for ix in centers:
                p = int(round(rpix[0] + ix * spacing_pixels * numpy.sign(model.wcs.wcs.cdelt[0]))), \
                    int(round(rpix[1] + iy * spacing_pixels * numpy.sign(model.wcs.wcs.cdelt[1])))
                sc = pixel_to_skycoord(p[0], p[1], model.wcs, origin=1)
                comp = create_skycomponent(flux=flux, frequency=frequency, direction=sc,
                                           polarisation_frame=PolarisationFrame("stokesI"))
                comps.append(comp)
        if block:
            predict_skycomponent_blockvisibility(vt, comps)
        else:
            predict_skycomponent_visibility(vt, comps)
        insert_skycomponent(model, comps)
        self.model = copy_image(model)
        self.empty_model = create_empty_image_like(model)
        
        export_image_to_fits(model, '%s/test_bags_model.fits' % (self.results_dir))
        
        if add_errors:
            # These will be the same for all calls
            numpy.random.seed(180555)
            gt = create_gaintable_from_blockvisibility(vt)
            gt = simulate_gaintable(gt, phase_error=1.0, amplitude_error=0.0)
            vt = apply_gaintable(vt, gt)
        return vt
    
    def get_LSM(self, vt, cellsize=0.001, reffrequency=None, flux=0.0):
        if reffrequency is None:
            reffrequency = [1e8]
        model = create_image_from_visibility(vt, npixel=self.npixel, cellsize=cellsize, npol=1,
                                             frequency=reffrequency,
                                             polarisation_frame=PolarisationFrame("stokesI"))
        model.data[..., 31, 31] = flux
        return model

    def test_predict_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = create_predict_graph(zero_vis_graph_list, flux_model_graph,
                                                        vis_slices=self.vis_slices)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.5, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 100.064844507, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1567.25, 0)
    
    def test_predict_wstack_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = create_predict_graph(zero_vis_graph_list, flux_model_graph,
                                                        context='wstack_single',
                                                        vis_slices=self.vis_slices)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.5, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 100.064844507, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1669.7, 0)
    
    def test_predict_timeslice_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = create_predict_graph(zero_vis_graph_list, flux_model_graph,
                                                        context='timeslice_single',
                                                        vis_slices=self.ntimes)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.5, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 99.763391030976067, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1546.1, 0)
    
    def test_predict_timeslice_graph_wprojection(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = \
            create_predict_graph(zero_vis_graph_list, flux_model_graph, vis_slices=3,
                                 context='timeslice_single', kernel='wprojection', wstep=4.0)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1600.0, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 165.5, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1709.4, 0)
    
    def test_predict_facet_wstack_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = create_predict_facet_graph(zero_vis_graph_list,
                                                              flux_model_graph,
                                                              facets=2,
                                                              context='wstack_single',
                                                              vis_slices=self.vis_slices)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.5, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 100.064844507, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1559.4, 0)
    
    def test_predict_wstack_graph_wprojection(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = \
            create_predict_graph(zero_vis_graph_list, flux_model_graph,
                                 vis_slices=11, wstep=10.0,
                                 context='wstack_single',
                                 kernel='wprojection')
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1600.0, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 100.064844507, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1668.3018405354974, 0)
    
    def test_predict_facet_timeslice_graph_wprojection(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = \
            create_predict_facet_graph(zero_vis_graph_list, flux_model_graph,
                                       vis_slices=3, facets=2,
                                       context='timeslice',
                                       wstep=4.0, kernel='wprojection')
        simple_vis(predicted_vis_graph_list[0], filename='predict_facet_timeslice_graph_wprojection', format='png')
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1600.0, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 94.2, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1656.6, 0)
    
    def test_predict_wprojection_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = \
            create_predict_graph(zero_vis_graph_list, flux_model_graph, wstep=10.0,
                                 context='2d', kernel='wprojection')
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list, predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.5, 0)
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 111.8, 0)
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1573.9, 0)
    
    def test_predict_facet_graph(self):
        flux_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                 flux=100.0)
        zero_vis_graph_list = create_zero_vis_graph_list(self.vis_graph_list)
        predicted_vis_graph_list = create_predict_facet_graph(zero_vis_graph_list, flux_model_graph,
                                                              facets=self.facets)
        residual_vis_graph_list = create_subtract_vis_graph_list(self.vis_graph_list,
                                                                 predicted_vis_graph_list)
        if self.compute:
            qa = qa_visibility(self.vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1593.52, 0)
            
            qa = qa_visibility(predicted_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 100.064844507, 0)
            
            qa = qa_visibility(residual_vis_graph_list[0].compute())
            numpy.testing.assert_almost_equal(qa.data['maxabs'], 1555.3, 0)
    
    def test_invert_graph(self):
        
        dirty_graph = create_invert_graph(self.vis_graph_list, self.model_graph, context='2d',
                                          dopsf=False, normalize=True)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_graph_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 94.8) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.3) < 1.0, str(qa)
    
    def test_invert_graph_wprojection(self):
        
        dirty_graph = create_invert_graph(self.vis_graph_list, self.model_graph, context='2d',
                                          dopsf=False, normalize=True,
                                          kernel='wprojection', wstep=10.0)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_graph_wprojection_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.3) < 1.0, str(qa)
    
    def test_invert_facet_graph(self):
        
        dirty_graph = create_invert_facet_graph(self.vis_graph_list, self.model_graph,
                                                context='2d', vis_slices=None,
                                                dopsf=False, normalize=True, facets=4)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_facet_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.9) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_invert_wstack_graph(self):
        
        dirty_graph = \
            create_invert_graph(self.vis_graph_list, self.model_graph,
                                context='wstack_single',
                                dopsf=False, normalize=True,
                                vis_slices=self.vis_slices)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_wstack_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_invert_timeslice_graph_wprojection(self):
        # Broken: gives enlarged blob at roughy correct locations
        
        dirty_graph = \
            create_invert_graph(self.vis_graph_list, self.model_graph,
                                context='timeslice_single',
                                dopsf=False, normalize=True,
                                vis_slices=3, kernel='wprojection',
                                wstep=10.0)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_timeslice_wprojection_dirty.fits' % (
                self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 3.5) < 1.0, str(qa)
    
    def test_invert_timeslice_graph(self):
        
        dirty_graph = \
            create_invert_graph(self.vis_graph_list, self.model_graph,
                                dopsf=False, normalize=True,
                                context='timeslice_single',
                                vis_slices=self.ntimes)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_timeslice_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_invert_facet_wstack_graph(self):
        
        dirty_graph = \
            create_invert_facet_graph(self.vis_graph_list, self.model_graph,
                                      context='wstack',
                                      dopsf=False, normalize=True,
                                      vis_slices=self.vis_slices, facets=4)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_facet_wstack_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 3.5) < 1.0, str(qa)
    
    def test_invert_facet_wstack_graph_wprojection(self):
        
        dirty_graph = create_invert_facet_graph(self.vis_graph_list, self.model_graph,
                                                dopsf=False, normalize=True,
                                                vis_slices=self.vis_slices, facets=2,
                                                context='wstack_single',
                                                kernel='wprojection', wstep=4.0)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_facet_wstack_wprojection_dirty.fits' % (
                self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.8) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_invert_facet_timeslice_graph(self):
        # Broken: gives enlarged blob at roughy correct locations
        
        dirty_graph = \
            create_invert_facet_graph(self.vis_graph_list, self.model_graph,
                                      context='timeslice_single',
                                      dopsf=False, normalize=True,
                                      vis_slices=self.ntimes, facets=2)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_facet_timeslice_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 3.5) < 1.0, str(qa)
    
    def test_invert_facet_timeslice_graph_wprojection(self):
        
        dirty_graph = \
            create_invert_facet_graph(self.vis_graph_list, self.model_graph,
                                      dopsf=False, normalize=True,
                                      context='timeslice_single',
                                      vis_slices=self.ntimes, facets=2,
                                      kernel='wprojection', wstep=4.0)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_facet_timeslice_wprojection_dirty.fits' % (
                self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_invert_wstack_graph_wprojection(self):
        
        dirty_graph = \
            create_invert_graph(self.vis_graph_list, self.model_graph,
                                context='wstack_single',
                                dopsf=False, normalize=True,
                                vis_slices=self.vis_slices // 3,
                                wstep=10.0,
                                kernel='wprojection')
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_invert_wstack_wprojection_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.5) < 1.0, str(qa)
    
    def test_residual_facet_graph(self):
        
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                 flux=100.0)
        
        dirty_graph = create_residual_facet_graph(self.vis_graph_list, self.model_graph,
                                                  facets=self.facets)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_residual_facet%d.fits' %
                                 (self.results_dir, self.facets))
            
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)

    def test_residual_wstack_graph(self):
    
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                 flux=100.0)
    
        dirty_graph = create_residual_graph(self.vis_graph_list, self.model_graph,
                                            context='wstack_single',
                                            vis_slices=self.vis_slices)
    
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_residual_wstack_slices%d.fits' %
                                 (self.results_dir, self.vis_slices))
        
            qa = qa_image(dirty[0])
        
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)

    def test_residual_timeslice_graph(self):
    
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                 flux=100.0)
    
        dirty_graph = create_residual_graph(self.vis_graph_list, self.model_graph,
                                            context='timeslice_single',
                                            vis_slices=self.ntimes)
    
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_residual_timeslice.fits' %
                                 (self.results_dir))
        
            qa = qa_image(dirty[0])
        
            assert numpy.abs(qa.data['max'] - 100.5) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 4.7) < 1.0, str(qa)

    def test_residual_facet_wstack_graph(self):
        
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                 flux=100.0)
        
        dirty_graph = \
            create_residual_facet_graph(self.vis_graph_list, self.model_graph,
                                        context='wstack_single',
                                        facets=4, vis_slices=self.vis_slices)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_residual_wstack_slices%d.fits' %
                                 (self.results_dir, self.vis_slices))
            
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 100.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.4) < 1.0, str(qa)
    
    def test_residual_wstack_graph_wprojection(self):
        
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], flux=100.0)
        
        dirty_graph = \
            create_residual_graph(self.vis_graph_list, self.model_graph,
                                  context='wstack_single',
                                  kernel='wprojection', vis_slices=self.vis_slices // 3,
                                  wstep=10.0)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_residual_wprojection.fits' %
                                 (self.results_dir))
            
            qa = qa_image(dirty[0])
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 2.5) < 1.0, str(qa)
    
    def test_deconvolution_facet_graph(self):
        
        facets = 4
        model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                            flux=0.0)
        dirty_graph = create_invert_graph(self.vis_graph_list, model_graph,
                                                 context='wstack_single',
                                                 dopsf=False, vis_slices=self.vis_slices)
        psf_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                flux=0.0)
        psf_graph = create_invert_graph(self.vis_graph_list, psf_model_graph,
                                               context='wstack_single',
                                               vis_slices=self.vis_slices,
                                               dopsf=True)
        
        clean_graph = create_deconvolve_graph(dirty_graph, psf_graph, model_graph,
                                                    algorithm='hogbom', niter=1000,
                                                    fractional_threshold=0.02, threshold=2.0,
                                                    gain=0.1, facets=facets)
        if self.compute:
            result = clean_graph.compute()
            
            export_image_to_fits(result, '%s/test_imaging_deconvolution_facets%d.clean.fits' %
                                 (self.results_dir, facets))
            
            qa = qa_image(result)
            
            assert numpy.abs(qa.data['max'] - 98.9) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 1.8) < 1.0, str(qa)
    
    @unittest.skip("Not yet ready")
    def test_deconvolution_channel_graph(self):
        
        self.vis_graph_list = self.setupVis(freqwin=8)
        self.model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2], frequency=self.frequency)
        
        model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                            flux=0.0)
        dirty_graph = create_invert_graph(self.vis_graph_list, model_graph,
                                                 dopsf=False, vis_slices=self.vis_slices)
        context = 'wstack_single',
        psf_model_graph = delayed(self.get_LSM)(self.vis_graph_list[self.nvis // 2],
                                                flux=0.0)
        psf_graph = create_invert_graph(self.vis_graph_list, psf_model_graph,
                                               context='wstack_single',
                                               vis_slices=self.vis_slices,
                                               dopsf=True)
        
        channel_images = 4
        clean_graph = create_deconvolve_channel_graph(dirty_graph, psf_graph, model_graph,
                                                      algorithm='hogbom', niter=1000,
                                                      fractional_threshold=0.02, threshold=2.0,
                                                      gain=0.1, subimages=channel_images)
        if self.compute:
            result = clean_graph.compute()
            
            export_image_to_fits(result, '%s/test_imaging_deconvolution_channels%d.clean.fits' %
                                 (self.results_dir, channel_images))
            
            qa = qa_image(result)
            
            assert numpy.abs(qa.data['max'] - 100.1) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 1.8) < 1.0, str(qa)
    
    def test_selfcal_global_graph(self):
        
        corrupted_vis_graph_list = self.setupVis(add_errors=True)
        
        selfcal_vis_graph_list = \
            create_selfcal_graph_list(corrupted_vis_graph_list,
                                                           delayed(self.model),
                                                           global_solution=True,
                                                           context='wstack_single',
                                                           vis_slices=self.vis_slices)
        
        dirty_graph = create_invert_graph(selfcal_vis_graph_list, self.model_graph,
                                          context='wstack_single',
                                          dopsf=False, normalize=True,
                                          vis_slices=self.vis_slices)
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_graphs_global_selfcal_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 3.5) < 1.0, str(qa)
    
    def test_selfcal_nonglobal_graph(self):
        
        corrupted_vis_graph_list = self.setupVis(add_errors=True)
        
        selfcal_vis_graph_list = create_selfcal_graph_list(corrupted_vis_graph_list,
                                                           delayed(self.model),
                                                           global_solution=False,
                                                           context='wstack_single',
                                                           vis_slices=self.vis_slices)
        
        dirty_graph = create_invert_graph(selfcal_vis_graph_list, self.model_graph,
                                          context='wstack_single',
                                          dopsf=False, normalize=True,
                                          vis_slices=self.vis_slices)
        
        if self.compute:
            dirty = dirty_graph.compute()
            export_image_to_fits(dirty[0], '%s/test_imaging_graphs_nonglobal_selfcal_dirty.fits' % (self.results_dir))
            qa = qa_image(dirty[0])
            
            assert numpy.abs(qa.data['max'] - 101.7) < 1.0, str(qa)
            assert numpy.abs(qa.data['min'] + 3.5) < 1.0, str(qa)