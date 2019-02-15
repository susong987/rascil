# Pipeline timings, using Dask
#
# This takes command line arguments and runs simulation and data reduction, outputing a CSV file of various
# timings.
#
import logging
import pprint
import socket
import time

import numpy
from astropy import units as u
from astropy.coordinates import SkyCoord

from data_models.polarisation import PolarisationFrame
from processing_library.util.sizeof import get_size
from workflows.arlexecute.imaging.imaging_arlexecute import invert_list_arlexecute_workflow, \
    weight_list_arlexecute_workflow, \
    taper_list_arlexecute_workflow, remove_sumwt
from workflows.arlexecute.pipelines.pipeline_arlexecute import ical_list_arlexecute_workflow
from workflows.arlexecute.simulation.simulation_arlexecute import simulate_list_arlexecute_workflow, \
    corrupt_list_arlexecute_workflow
from workflows.arlexecute.skymodel.skymodel_arlexecute import predict_skymodel_list_arlexecute_workflow
from wrappers.arlexecute.calibration.calibration_control import create_calibration_controls
from wrappers.arlexecute.execution_support.arlexecute import arlexecute
from wrappers.arlexecute.execution_support.dask_init import findNodes, get_dask_Client
from wrappers.arlexecute.griddata.kernels import create_awterm_convolutionfunction
from wrappers.arlexecute.image.gather_scatter import image_gather_channels
from wrappers.arlexecute.image.operations import export_image_to_fits, qa_image
from wrappers.arlexecute.imaging.base import create_image_from_visibility, advise_wide_field
from wrappers.arlexecute.simulation.testing_support import create_low_test_skymodel_from_gleam
from wrappers.arlexecute.visibility.coalesce import convert_blockvisibility_to_visibility

pp = pprint.PrettyPrinter()


def git_hash():
    """ Get the hash for this git repository.
    
    Requires that the code tree was created using git
    
    :return: string or "unknown"
    """
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", 'HEAD'])
    except Exception as excp:
        print(excp)
        return "unknown"


def trial_case(results, seed=180555, context='wstack', nworkers=8, threads_per_worker=1, memory=8,
               processes=True, order='frequency', nfreqwin=7, ntimes=3, rmax=750.0,
               facets=1, wprojection_planes=1, use_dask=True, distribute_imaging=True,
               flux_limit=0.3, nmajor=5, dft_threshold=1.0, distribute_clean=True,
               write_fits=False):
    """ Single trial for performance-timings
    
    Simulates visibilities from GLEAM including phase errors
    Makes dirty image and PSF
    Runs ICAL pipeline
    
    The results are in a dictionary:
    
    'context': input - a string describing concisely the purpose of the test
    'time overall',  overall execution time (s)
    'time create gleam', time to create GLEAM prediction graph
    'time predict', time to execute GLEAM prediction graph
    'time corrupt', time to corrupt data_models
    'time invert', time to make dirty image
    'time ICAL graph', time to create ICAL graph
    'time ICAL', time to execute ICAL graph
    'context', type of imaging e.g. 'wstack'
    'nworkers', number of workers to create
    'threads_per_worker',
    'nnodes', Number of nodes,
    'processes', 'order', Ordering of data_models
    'nfreqwin', Number of frequency windows in simulation
    'ntimes', Number of hour angles in simulation
    'rmax', Maximum radius of stations used in simulation (m)
    'facets', Number of facets in deconvolution and imaging
    'wprojection_planes', Number of wprojection planes
    'vis_slices', Number of visibility slices (per Visibbility)
    'npixel', Number of pixels in image
    'cellsize', Cellsize in radians
    'seed', Random number seed
    'dirty_max', Maximum in dirty image
    'dirty_min', Minimum in dirty image
    'restored_max',
    'restored_min',
    'deconvolved_max',
    'deconvolved_min',
    'residual_max',
    'residual_min',
    'git_info', GIT hash (not definitive since local mods are possible)
    
    :param results: Initial state
    :param seed: Random number seed (used in gain simulations)
    :param context: imaging context
    :param context: Type of context: '2d'|'timeslice'|'wstack'
    :param nworkers: Number of dask workers to use
    :param threads_per_worker: Number of threads per worker
    :param processes: Use processes instead of threads 'processes'|'threads'
    :param order: See simulate_list_list_arlexecute_workflow_workflowkflow
    :param nfreqwin: See simulate_list_list_arlexecute_workflow_workflowkflow
    :param ntimes: See simulate_list_list_arlexecute_workflow_workflowkflow
    :param rmax: See simulate_list_list_arlexecute_workflow_workflowkflow
    :param facets: Number of facets to use
    :param wprojection_planes: Number of wprojection planes to use
    :param use_dask: Use dask or immediate evaluation
    :return: results dictionary
    """
    if use_dask:
        client = get_dask_Client(threads_per_worker=threads_per_worker,
                                 processes=threads_per_worker == 1,
                                 memory_limit=memory * 1024 * 1024 * 1024,
                                 n_workers=nworkers)
        arlexecute.set_client(client)
        nodes = findNodes(arlexecute.client)
        print("Defined %d workers on %d nodes" % (nworkers, len(nodes)))
        print("Workers are: %s" % str(nodes))
    else:
        arlexecute.set_client(use_dask=use_dask)
        results['nnodes'] = 1
    
    def init_logging():
        logging.basicConfig(filename='pipelines_arlexecute_timings.log',
                            filemode='a',
                            format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%H:%M:%S',
                            level=logging.INFO)
    
    init_logging()
    log = logging.getLogger()
    
    # Initialise logging on the workers. This appears to only work using the process scheduler.
    arlexecute.run(init_logging)
    
    def lprint(*args):
        log.info(*args)
        print(*args)
    
    lprint("Starting pipelines_arlexecute_timings")
    
    numpy.random.seed(seed)
    results['seed'] = seed
    
    start_all = time.time()
    
    results['context'] = context
    results['hostname'] = socket.gethostname()
    results['git_hash'] = git_hash()
    results['epoch'] = time.strftime("%Y-%m-%d %H:%M:%S")
    
    lprint("Context is %s" % context)
    
    results['nworkers'] = nworkers
    results['threads_per_worker'] = threads_per_worker
    results['processes'] = processes
    results['memory'] = memory
    results['order'] = order
    results['nfreqwin'] = nfreqwin
    results['ntimes'] = ntimes
    results['rmax'] = rmax
    results['facets'] = facets
    results['wprojection_planes'] = wprojection_planes
    results['dft threshold'] = dft_threshold
    
    results['use_dask'] = use_dask
    
    lprint("At start, configuration is:")
    lprint(results)
    
    # Parameters determining scale of simulation.
    frequency = numpy.linspace(1.0e8, 1.2e8, nfreqwin)
    centre = nfreqwin // 2
    if nfreqwin > 1:
        channel_bandwidth = numpy.array(nfreqwin * [frequency[1] - frequency[0]])
    else:
        channel_bandwidth = numpy.array([1e6])
    
    times = numpy.linspace(-numpy.pi / 4.0, numpy.pi / 4.0, ntimes)
    phasecentre = SkyCoord(ra=+0.0 * u.deg, dec=-40.0 * u.deg, frame='icrs', equinox='J2000')

    lprint("****** Visibility creation ******")
    # Create the empty BlockVisibility's and persist these on the cluster
    bvis_list = simulate_list_arlexecute_workflow('LOWBD2',
                                                  frequency=frequency,
                                                  channel_bandwidth=channel_bandwidth,
                                                  times=times,
                                                  phasecentre=phasecentre,
                                                  order=order,
                                                  format='blockvis',
                                                  rmax=rmax)
    bvis_list = arlexecute.persist(bvis_list)
    
    # We also need the Visibility versions
    vis_list = [arlexecute.execute(convert_blockvisibility_to_visibility)(bv) for bv in bvis_list]
    future_vis_list = arlexecute.persist(vis_list)

    # Find the best imaging parameters but don't bring the vis_list back here
    def get_wf(v):
        return advise_wide_field(v, guard_band_image=6.0, delA=0.1, facets=facets,
                                 wprojection_planes=wprojection_planes, oversampling_synthesised_beam=4.0)
    
    
    future_advice = [arlexecute.execute(get_wf)(v) for v in future_vis_list]
    advice = arlexecute.compute(future_advice, sync=True)[-1]
    # Deconvolution via sub-images requires 2^n
    npixel = advice['npixels2']
    results['npixel'] = npixel
    cellsize = advice['cellsize']
    results['cellsize'] = cellsize
    lprint("Image will have %d by %d pixels, cellsize = %.6f rad" % (npixel, npixel, cellsize))
    
    # Create an empty model image
    future_model_list = [arlexecute.execute(create_image_from_visibility)
                  (vis_list[f],
                   npixel=npixel, cellsize=cellsize,
                   frequency=[frequency[f]],
                   channel_bandwidth=[channel_bandwidth[f]],
                   polarisation_frame=PolarisationFrame("stokesI"))
                  for f, freq in enumerate(frequency)]
    
    start = time.time()
    lprint("****** Starting weighting and tapering ******")
    future_vis_list = weight_list_arlexecute_workflow(future_vis_list, future_model_list)
    future_vis_list = taper_list_arlexecute_workflow(future_vis_list, 0.003 * 750.0 / rmax)
    lprint('Size of weighting and tapering graph is %.3E bytes' % (get_size(vis_list)))
    future_vis_list = arlexecute.compute(future_vis_list, sync=True)
    future_vis_list = arlexecute.scatter(future_vis_list)
    end = time.time()
    results['time weight'] = end - start
    lprint("Weighting and tapering took %.3f seconds" % (end - start))
    
    lprint("****** Setting up imaging parameters ******")
    # Now set up the imaging parameters
    gcfcf = None
    
    if context == 'timeslice':
        vis_slices = ntimes
        lprint("Using timeslice with %d slices" % vis_slices)
    elif context == '2d':
        vis_slices = 1
    elif context == "wprojection":
        wstep = advice['wstep']
        nw = advice['wprojection_planes']
        vis_slices = 1
        support = advice['nwpixels']
        results['wprojection_planes'] = nw
        
        start = time.time()
        lprint("****** Starting W projection kernel creation ******")
        lprint("Using wprojection with %d planes with wstep %.1f wavelengths" % (nw, wstep))
        lprint("Support of wprojection = %d pixels" % support)
        template_model = create_image_from_visibility(vis_list[centre],
                                                      npixel=npixel, cellsize=cellsize,
                                                      frequency=[frequency[centre]],
                                                      channel_bandwidth=[channel_bandwidth[centre]],
                                                      polarisation_frame=PolarisationFrame("stokesI"))
        gcfcf = create_awterm_convolutionfunction(template_model, nw=nw, wstep=wstep, oversampling=4,
                                                  support=support, use_aaf=True)
        lprint("Size of W projection gcf, cf = %.2E bytes" % get_size(gcfcf))
        end = time.time()
        results['time create wprojection'] = end - start
    else:
        context = 'wstack'
        vis_slices = advice['vis_slices']
        lprint("Using wstack with %d slices" % vis_slices)
    
    future_gcfcf_list = arlexecute.scatter(nfreqwin * [gcfcf])
    
    results['vis_slices'] = vis_slices
    
    # Make a skymodel from gleam, with bright sources as components and weak sources in an image
    lprint("****** Starting GLEAM skymodel creation ******")
    start = time.time()
    future_skymodel_list = [arlexecute.execute(create_low_test_skymodel_from_gleam)
                     (npixel=npixel, cellsize=cellsize, frequency=[frequency[f]],
                      phasecentre=phasecentre,
                      polarisation_frame=PolarisationFrame("stokesI"),
                      flux_limit=flux_limit,
                      flux_threshold=dft_threshold,
                      flux_max=5.0) for f, freq in enumerate(frequency)]
    lprint('Size of skymodel creation graph is %.3E bytes' % (get_size(future_skymodel_list)))
    skymodel_list = arlexecute.compute(future_skymodel_list, sync=True)
    end = time.time()
    lprint("GLEAM skymodel creation took %.3f seconds" % (end - start))
    results['time create gleam'] = end - start
    
    # We use predict_skymodel so that we can use skycomponents as well as images
    lprint("****** Starting GLEAM skymodel prediction ******")
    future_skymodel_list = arlexecute.scatter(skymodel_list)
    start = time.time()
    predicted_vis_list = [predict_skymodel_list_arlexecute_workflow(future_vis_list[f],
                                                                    [future_skymodel_list[f]],
                                                                    context=context,
                                                                    vis_slices=vis_slices, facets=facets,
                                                                    gcfcf=[future_gcfcf_list[f]])[0]
                          for f, freq in enumerate(frequency)]
    
    lprint('Size of skymodel prediction graph is %.3E bytes' % (get_size(predicted_vis_list)))
    predicted_vis_list = arlexecute.compute(predicted_vis_list, sync=True)
    end = time.time()
    lprint("GLEAM skymodel prediction took %.3f seconds" % (end - start))
    results['time predict gleam'] = end - start
    future_predicted_vis_list = arlexecute.scatter(predicted_vis_list)

    lprint("****** Starting dirty image calculation ******")
    start = time.time()
    dirty_list = invert_list_arlexecute_workflow(future_predicted_vis_list, future_model_list,
                                                 vis_slices=vis_slices,
                                                 context=context, facets=facets,
                                                 use_serial_invert=not distribute_imaging,
                                                 gcfcf=future_gcfcf_list)
    lprint('Size of dirty graph is %.3E bytes' % (get_size(dirty_list)))
    dirty, sumwt = arlexecute.compute(dirty_list, sync=True)[centre]
    end = time.time()
    results['time invert'] = end - start
    lprint("Dirty image invert took %.3f seconds" % (end - start))
    lprint("Maximum in dirty image is %f, sumwt is %s" % (numpy.max(numpy.abs(dirty.data)), str(sumwt)))
    qa = qa_image(dirty)
    results['dirty_max'] = qa.data['max']
    results['dirty_min'] = qa.data['min']
    if write_fits:
        export_image_to_fits(dirty, "pipelines_arlexecute_timings-%s-dirty.fits" % context)
    
    # Corrupt the visibility for the GLEAM model
    lprint("****** Visibility corruption ******")
    start = time.time()
    future_corrupted_vis_list = corrupt_list_arlexecute_workflow(future_predicted_vis_list, phase_error=1.0, seed=seed)
    lprint('Size of visibility corruption graph is %.3E bytes' % (get_size(future_corrupted_vis_list)))
    corrupted_vis_list = arlexecute.compute(future_corrupted_vis_list, sync=True)
    end = time.time()
    results['time corrupt'] = end - start
    lprint("Visibility corruption took %.3f seconds" % (end - start))
    future_corrupted_vis_list = arlexecute.scatter(corrupted_vis_list)

    # Create the ICAL pipeline to run major cycles, starting selfcal at cycle 1. A global solution across all
    # frequencies (i.e. Visibilities) is performed.
    
    controls = create_calibration_controls()
    
    controls['T']['first_selfcal'] = 1
    controls['T']['timescale'] = 'auto'
    
    if distribute_clean:
        print("Using distributed clean")
        deconvolve_facets = 8
        deconvolve_overlap = 16
        deconvolve_taper = 'tukey'
    else:
        print("Using single thread clean")
        deconvolve_facets = 1
        deconvolve_overlap = 0
        deconvolve_taper = 'tukey'

    lprint("****** Starting ICAL graph creation ******")
    start = time.time()
    ical_list = ical_list_arlexecute_workflow(future_corrupted_vis_list,
                                              model_imagelist=future_model_list,
                                              context=context,
                                              vis_slices=vis_slices,
                                              scales=[0, 3, 10],
                                              algorithm='mmclean',
                                              nmoment=3, niter=1000,
                                              fractional_threshold=0.1,
                                              threshold=0.01, nmajor=nmajor,
                                              gain=0.25,
                                              psf_support=64,
                                              deconvolve_facets=deconvolve_facets,
                                              deconvolve_overlap=deconvolve_overlap,
                                              deconvolve_taper=deconvolve_taper,
                                              timeslice='auto',
                                              global_solution=True,
                                              do_selfcal=True,
                                              calibration_context='T',
                                              controls=controls,
                                              use_serial_predict=not distribute_imaging,
                                              use_serial_invert=not distribute_imaging,
                                              gcfcf=future_gcfcf_list)
    
    results['size ICAL graph'] = get_size(ical_list)
    lprint('Size of ICAL graph is %.3E bytes' % results['size ICAL graph'])
    end = time.time()
    results['time ICAL graph'] = end - start
    lprint("Construction of ICAL graph took %.3f seconds" % (end - start))
    
    # Execute the graph
    lprint("****** Executing ICAL graph ******")
    start = time.time()
    deconvolved, residual, restored, gaintables = arlexecute.compute(ical_list, sync=True)
    end = time.time()
    
    results['time ICAL'] = end - start
    lprint("ICAL graph execution took %.3f seconds" % (end - start))
    qa = qa_image(deconvolved[centre])
    results['deconvolved_max'] = qa.data['max']
    results['deconvolved_min'] = qa.data['min']
    deconvolved_cube = image_gather_channels(deconvolved)
    if write_fits:
        export_image_to_fits(deconvolved_cube, "pipelines_arlexecute_timings-%s-ical_deconvolved.fits" % context)
    
    qa = qa_image(residual[centre][0])
    results['residual_max'] = qa.data['max']
    results['residual_min'] = qa.data['min']
    residual_cube = remove_sumwt(residual)
    residual_cube = image_gather_channels(residual_cube)
    if write_fits:
        export_image_to_fits(residual_cube, "pipelines_arlexecute_timings-%s-ical_residual.fits" % context)
    
    qa = qa_image(restored[centre])
    results['restored_max'] = qa.data['max']
    results['restored_min'] = qa.data['min']
    restored_cube = image_gather_channels(restored)
    if write_fits:
        export_image_to_fits(restored_cube, "pipelines_arlexecute_timings-%s-ical_restored.fits" % context)
    #
    arlexecute.close()
    
    end_all = time.time()
    results['time overall'] = end_all - start_all
    
    lprint("At end, results are:")
    lprint(results)
    
    return results


def write_results(filename, fieldnames, results):
    with open(filename, 'a') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=',', quotechar='|',
                                quoting=csv.QUOTE_MINIMAL)
        writer.writerow(results)
        csvfile.close()


def write_header(filename, fieldnames):
    with open(filename, 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=',', quotechar='|',
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        csvfile.close()


def main(args):
    results = {}
    
    results['jobid'] = args.jobid
    
    nworkers = args.nworkers
    results['nworkers'] = nworkers
    
    context = args.context
    results['context'] = context
    
    nnodes = args.nnodes
    results['nnodes'] = nnodes
    
    threads_per_worker = args.nthreads
    
    print("Using %s workers" % nworkers)
    print("Using %s threads per worker" % threads_per_worker)
    
    nfreqwin = args.nfreqwin
    results['nfreqwin'] = nfreqwin
    
    rmax = args.rmax
    results['rmax'] = rmax
    
    flux_limit = args.flux_limit
    results['flux_limit'] = flux_limit
    
    dft_threshold = args.dft_threshold
    results['dft threshold'] = dft_threshold
    
    context = args.context
    results['context'] = context
    
    memory = args.memory
    results['memory'] = memory
    
    ntimes = args.ntimes
    results['ntimes'] = ntimes
    
    nmajor = args.nmajor
    results['nmajor'] = nmajor
    
    results['hostname'] = socket.gethostname()
    results['epoch'] = time.strftime("%Y-%m-%d %H:%M:%S")
    results['driver'] = 'pipelines_arlexecute_timings'
    
    use_dask = args.use_dask == 'True'
    if use_dask:
        distribute_imaging = args.distribute_imaging == 'True'
        print("Using Dask")
    else:
        distribute_imaging = False
    results['distribute_imaging'] = distribute_imaging
    if distribute_imaging:
        print("Using distributed imaging")
    else:
        print("Using serial imaging")

    distribute_clean = args.distribute_clean == 'True'
    results['distribute_clean'] = distribute_clean

    threads_per_worker = args.nthreads
    
    write_fits = args.write_fits == 'True'
    
    print("Defining %d frequency windows" % nfreqwin)
    
    fieldnames = ['cellsize',
                  'context',
                  'deconvolved_max',
                  'deconvolved_min',
                  'dft threshold',
                  'dirty_max',
                  'dirty_min',
                  'distribute_clean',
                  'distribute_imaging',
                  'driver',
                  'epoch',
                  'facets',
                  'flux_limit',
                  'git_hash',
                  'hostname',
                  'jobid',
                  'log_file',
                  'memory',
                  'nfreqwin',
                  'nmajor',
                  'nnodes',
                  'npixel',
                  'ntimes',
                  'nworkers',
                  'order',
                  'processes',
                  'residual_max',
                  'residual_min',
                  'restored_max',
                  'restored_min',
                  'rmax',
                  'seed',
                  'size ICAL graph',
                  'threads_per_worker',
                  'time ICAL',
                  'time ICAL graph',
                  'time corrupt',
                  'time create gleam',
                  'time create wprojection',
                  'time invert',
                  'time overall',
                  'time predict gleam',
                  'time weight',
                  'use_dask',
                  'vis_slices',
                  'wprojection_planes']
        
    filename = seqfile.findNextFile(prefix='%s_%s_' % (results['driver'], results['hostname']), suffix='.csv')
    print('Saving results to %s' % filename)
    
    write_header(filename, fieldnames)
    
    results = trial_case(results, use_dask=use_dask, nworkers=nworkers, rmax=rmax, context=context, memory=memory,
                         threads_per_worker=threads_per_worker, nfreqwin=nfreqwin, ntimes=ntimes,
                         distribute_imaging=distribute_imaging, flux_limit=flux_limit, nmajor=nmajor,
                         dft_threshold=dft_threshold, distribute_clean=distribute_clean,
                         write_fits=write_fits)
    write_results(filename, fieldnames, results)
    
    print('Exiting %s' % results['driver'])


if __name__ == '__main__':
    import csv
    import seqfile
    
    import argparse
    
    parser = argparse.ArgumentParser(description='Benchmark pipelines in numpy and dask')
    parser.add_argument('--use_dask', type=str, default='True', help='Use Dask?')
    parser.add_argument('--nnodes', type=int, default=1, help='Number of nodes')
    parser.add_argument('--nthreads', type=int, default=1, help='Number of threads')
    parser.add_argument('--memory', type=int, default=8, help='Memory per worker')
    parser.add_argument('--nworkers', type=int, default=1, help='Number of workers')
    parser.add_argument('--nmajor', type=int, default=5, help='Number of major cycles')
    
    parser.add_argument('--ntimes', type=int, default=7, help='Number of hour angles')
    parser.add_argument('--nfreqwin', type=int, default=16, help='Number of frequency windows')
    parser.add_argument('--context', type=str, default='wstack',
                        help='Imaging context: 2d|timeslice|wstack')
    parser.add_argument('--rmax', type=float, default=750.0, help='Maximum baseline (m)')
    parser.add_argument('--distribute_imaging', type=str, default='False',
                        help='Use distributed imaging?')
    parser.add_argument('--distribute_clean', type=str, default='True',
                        help='Use distributed clean?')
    parser.add_argument('--jobid', type=int, default=0, help='JOBID from slurm')
    parser.add_argument('--flux_limit', type=float, default=0.3, help='Flux limit for components')
    parser.add_argument('--dft_threshold', type=float, default=1.0, help='Flux above which DFT is used')
    parser.add_argument('--log_file', type=str, default='pipelines_arlexecute_timings.log',
                        help='Name of output log file')
    parser.add_argument('--write_fits', type=str, default='False',
                        help='Write FITS files??')

    main(parser.parse_args())
    
    exit()