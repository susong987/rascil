#include <Python.h>
#include <stdio.h>
#include <assert.h>
#include <sys/types.h>
#include <sys/stat.h>
#include "/home/at748/Downloads/cfitsio/include/fitsio.h"

#include "arlwrap.h"


/* Simple exit-on-error */
void pycheck(PyObject *obj)
{
	if (!obj) {
		PyErr_Print();
		exit(1);
	}
}

/* In: module name, function name
 * Out: function address */
void *get_ffi_fn_addr(const char* module, const char* fn_name)
{
	PyObject *mod, *fn, *fn_addr;

	pycheck(mod = PyImport_ImportModule(module));
	pycheck(fn = PyObject_GetAttrString(mod, fn_name));
	pycheck(fn_addr = PyObject_GetAttrString(fn, "address"));

	return (void*)PyNumber_AsSsize_t(fn_addr, NULL);
}

/* DO NOT USE - we do not want PyObjects */
/* Leaving for reference only */
PyObject *get_plain_fn_addr(const char* module, const char* fn_name)
{
	PyObject *mod, *fn;

	pycheck(mod = PyImport_ImportModule(module));
	pycheck(fn = PyObject_GetAttrString(mod, fn_name));

	return fn;
}

/*
 * Verifies that:
 * - vt and vtmp are unique in memory
 * - vt and vtmp have equivalent values
 */
int verify_arl_copy(ARLVis *vt, ARLVis *vtmp)
{
	char *vtdata_bytes, *vtmpdata_bytes;
	int ARLVisDataSize;
	int i;

	if (vt == vtmp) {
		fprintf(stderr, "vt == vtmp\n");
		return 1;
	}

	if (!((vt->nvis == vtmp->nvis) && (vt->npol == vtmp->npol))) {
		return 2;
	}

	if (vt->data == vtmp->data) {
		return 3;
	}

	ARLVisDataSize = 72 + (32 * vt->npol * vt->nvis);
	vtdata_bytes = (char*) vt->data;
	vtmpdata_bytes = (char*) vtmp->data;

	for (i=0; i<ARLVisDataSize; i++) {
		if (vtdata_bytes[i] != vtmpdata_bytes[i]) {
			return 4;
		}
	}

	return 0;
}

/* Export image to FITS */
/* Assuming nx*ny*nfreq */
/* ToDo - add polarization and wcs */
int export_image_to_fits_c(Image *im, char * filename) {
	int status = 0, exists;
	fitsfile *fptr;       /* pointer to the FITS file; defined in fitsio.h */
	long  fpixel = 1, naxis = 4, nelements;
	long naxes[4];

	naxes[0] = im->data_shape[3];
	naxes[1] = im->data_shape[2];
	naxes[2] = im->data_shape[1];
	naxes[3] = im->data_shape[0];

	fits_file_exists(filename, &exists, &status); /* check if the file exists */

	if(exists != 0) {
		fits_open_file(&fptr, filename, READWRITE, &status); /* open existed file */
	}
	else {
		fits_create_file(&fptr, filename, &status);   /* create new file */
	}

	/* Create the primary array image  */
	fits_create_img(fptr, DOUBLE_IMG, naxis, naxes, &status);
	nelements = naxes[0] * naxes[1] * naxes[2] * naxes[3];          /* number of pixels to write */
	/* Write the array of integers to the image */
	fits_write_img(fptr, TDOUBLE, fpixel, nelements, im->data, &status);
	fits_close_file(fptr, &status);            /* close the file */
	fits_report_error(stderr, status);  /* print out any error messages */
	return status;
}

Image *allocate_image(int *shape)
{
	int i;
	Image *image = malloc(sizeof(Image));

	image->size = 1;//shape[0]*shape[1]*shape[2]*shape[3];

	for(i=0; i<4; i++) {
		image->data_shape[i] = shape[i];
		image->size *= shape[i];
	}

	image->data = calloc(image->size,sizeof(double));
	image->wcs = calloc(2996,sizeof(char));
	image->polarisation_frame = calloc(114,sizeof(char));

	return image;
}

void *destroy_image(Image *image)
{
	free(image->data);
	free(image->wcs);
	free(image->polarisation_frame);
	free(image);
	return NULL;
}


int main(int argc, char **argv)
{
	int *shape = malloc(4*sizeof(int));
	int serial_shape;
	int status;
	int nvis=1;

	double *times = calloc(1,sizeof(double));
	double *freq = malloc(1*sizeof(double));
	double *channel_bandwidth = malloc(1*sizeof(double));
	freq[0] = 1e8;
	channel_bandwidth[0] = 1e6;
	double cellsize = 0.0005;
	char config_name[] = "LOWBD2-CORE";

	ARLVis *vt = malloc(sizeof(ARLVis));
	ARLVis *vtmp = malloc(sizeof(ARLVis));

	ARLConf *lowconfig = malloc(sizeof(ARLConf));

	ant_t nb;

	Py_Initialize();

	vt->nvis = 13695;
	vt->npol = 1;

	// malloc to ARLDataVisSize
	//vt->data = malloc(72+(32*vt->npol*vt->nvis) * sizeof(char));
	//vtmp->data = malloc(72+(32*vt->npol*vt->nvis) * sizeof(char));
	vt->data = malloc(72+4*(32*vt->npol*vt->nvis) * sizeof(char));//13695
	vtmp->data = malloc(72+4*(32*vt->npol*vt->nvis) * sizeof(char));//13695

	if (!vt->data || !vtmp->data) {
		fprintf(stderr, "Malloc error\n");
		exit(1);
	}

	//arl_copy_visibility(vt, vtmp, false);

	//assert(0 == verify_arl_copy(vt, vtmp));

	helper_get_image_shape(freq, cellsize, shape);

	Image *model = allocate_image(shape);
	Image *m31image = allocate_image(shape);
	Image *dirty = allocate_image(shape);
	Image *psf = allocate_image(shape);
	Image *comp = allocate_image(shape);
	Image *residual = allocate_image(shape);
	Image *restored = allocate_image(shape);

	arl_create_visibility("LOWBD2-CORE", times, freq, channel_bandwidth, vt);

	arl_create_test_image(freq, cellsize, m31image);

	arl_predict_2d(vt, m31image, vtmp);

	
	free(vt->data);
	free(vt);
	vt = vtmp;
	vtmp = NULL;


	arl_create_image_from_visibility(vt, model);

	double *sumwt = malloc(sizeof(double));

	arl_invert_2d(vt, model, false, dirty, sumwt);
	arl_invert_2d(vt, model, true, psf, sumwt);

	arl_deconvolve_cube(dirty, psf, comp, residual);
	arl_restore_cube(comp, psf, residual, restored);

	// FITS files output
	status = mkdir("results", S_IRWXU | S_IRWXG | S_IROTH | S_IXOTH);
	status = export_image_to_fits_c(m31image, "results/m31image.fits");
	status = export_image_to_fits_c(dirty, "results/dirty.fits");
	status = export_image_to_fits_c(psf, "results/psf.fits");
	status = export_image_to_fits_c(residual, "results/residual.fits");
	status = export_image_to_fits_c(restored, "results/restored.fits");

	model = destroy_image(model);
	m31image = destroy_image(m31image);
	dirty = destroy_image(dirty);
	psf = destroy_image(psf);
	comp = destroy_image(comp);
	residual = destroy_image(residual);
	restored = destroy_image(restored);

	return 0;
}
