from __future__ import print_function
from sqgturb import SQG, rfft2, irfft2, RandomPattern
import numpy as np
from netCDF4 import Dataset
import sys, time, os
from sqgturb.enkf_utils import  cartdist,enkf_update,gaspcohn

# EnKF cycling for SQG turbulence model model with boundary temp obs,
# horizontal and vertical localization.  Relaxation to prior spread
# inflation, or Hodyss and Campbell inflation.
# Random or fixed observing network (obs on either boundary or
# both).

if len(sys.argv) == 1:
   msg="""
python sqg_enkf.py hcovlocal_scale covinflate1 covinflate2
   """
   raise SystemExit(msg)

# horizontal covariance localization length scale in meters.
hcovlocal_scale = float(sys.argv[1])
# vertical covariance localization factor
# if < 0, default used (vcovlocal_fact = L_r/hcovlocal_scale,
# where L_r is Rossby radius)
#vcovlocal_fact = float(sys.argv[2])
vcovlocal_fact = -1
# stochastic parameterization parameters
amp = np.asarray(eval(sys.argv[2]),np.float)
hcorr = np.asarray(eval(sys.argv[3]),np.float)
tcorr = np.asarray(eval(sys.argv[4]),np.float)
nsamples = 2
# inflation parameters
# (covinflate2 <= 0 for RTPS inflation
# (http://journals.ametsoc.org/doi/10.1175/MWR-D-11-00276.1),
# otherwise use Hodyss et al inflation
# (http://journals.ametsoc.org/doi/abs/10.1175/MWR-D-15-0329.1)
if len(sys.argv) > 5:
    covinflate1 = float(sys.argv[5])
    covinflate2 = -1
else:
    covinflate1 = 1.
    covinflate2 = 1.

diff_efold = None # use diffusion from climo file

savedata = None # if not None, netcdf filename to save data.
#savedata = 'sqg_enkf.nc'

profile = False # turn on profiling?

use_letkf = False # use serial EnSRF

# if nobs > 0, each ob time nobs ob locations are randomly sampled (without
# replacement) from the model grid
# if nobs < 0, fixed network of every Nth grid point used (N = -nobs)
nobs = 4096 # number of obs to assimilate (randomly distributed)
#nobs = -1 # fixed network, every -nobs grid points. nobs=-1 obs at all pts.

# if levob=0, sfc temp obs used.  if 1, lid temp obs used. If [0,1] obs at both
# boundaries.
levob = [0,1]; levob = list(levob); levob.sort()

direct_insertion = False # only relevant for nobs=-1, levob=[0,1]
if direct_insertion: print('# direct insertion!')

nanals = 40 # ensemble members

oberrstdev = 1.0 # ob error standard deviation in K

nassim = 440 # assimilation times to run
nassim_spinup = 80

filename_climo = '../examples/sqg_N128_3hrly.nc' # file name for forecast model climo
# perfect model
#filename_truth = '../examples/sqg_N128_3hrly.nc' # file name for nature run to draw obs
# truncated model
filename_truth = '../examples/sqg_N512_N128_3hrly_blockmean.nc' # file name for nature run to draw obs

print('# filename_modelclimo=%s' % filename_climo)
print('# filename_truth=%s' % filename_truth)

# fix random seed for reproducibility.
np.random.seed(42)

# get model info
nc_climo = Dataset(filename_climo)
# parameter used to scale PV to temperature units.
scalefact = nc_climo.f*nc_climo.theta0/nc_climo.g
# initialize qg model instances for each ensemble member.
x = nc_climo.variables['x'][:]
y = nc_climo.variables['y'][:]
pv_climo = nc_climo.variables['pv']
indxran = np.random.choice(pv_climo.shape[0],size=nanals,replace=False)
x, y = np.meshgrid(x, y)
nx = len(x); ny = len(y)
pvens = np.empty((nanals,2,ny,nx),np.float32)
dt = nc_climo.dt
if diff_efold == None: diff_efold=nc_climo.diff_efold
# get OMP_NUM_THREADS (threads to use) from environment.
threads = int(os.getenv('OMP_NUM_THREADS','1'))
if amp.all() == 0:
    rp = None
else:
    rp_norm = 'pv' # random pattern specified in pv (pot. temp) norm or streamfunction (psi) norm.
    if rp_norm == 'pv':
        stdev= amp/scalefact # amp given in units of K (for psi units are m**2/s)
    elif rp_norm == 'psi':
        stdev = amp # psi units are m**2/s
    else:
        raise ValueError('illegal random pattern norm')
    rp = RandomPattern(hcorr*nc_climo.L/nx,tcorr*dt,nc_climo.L,nx,dt,nsamples=nsamples,stdev=stdev,norm=rp_norm)
rpatterns = []; models = []
for nanal in range(nanals):
    pvens[nanal] = pv_climo[indxran[nanal]]
    if rp is not None:
        rpx = rp.copy(seed=nanal)
        rpatterns.append(rpx)
    else:
        rpx = None
    models.append(\
    SQG(pvens[nanal],random_pattern=rpx,\
    nsq=nc_climo.nsq,f=nc_climo.f,dt=dt,U=nc_climo.U,H=nc_climo.H,\
    r=nc_climo.r,tdiab=nc_climo.tdiab,symmetric=nc_climo.symmetric,\
    diff_order=nc_climo.diff_order,diff_efold=diff_efold,threads=threads))

# default vertical localization scale
Lr = np.sqrt(models[0].nsq)*models[0].H/models[0].f
if vcovlocal_fact < 0:
    vcovlocal_fact = gaspcohn(np.array(Lr/hcovlocal_scale))

print("# hcovlocal=%g vcovlocal=%s diff_efold=%s levob=%s covinf1=%s covinf2=%s nanals=%s" %\
     (hcovlocal_scale/1000.,vcovlocal_fact,diff_efold,levob,covinflate1,covinflate2,nanals))

# nature run
nc_truth = Dataset(filename_truth)
pv_truth = nc_truth.variables['pv']
# set up arrays for obs and localization function
if nobs < 0:
    nskip = -nobs
    if nx%nobs != 0:
        raise ValueError('nx must be divisible by nobs')
    nobs = (nx/nobs)**2
    print('# nobs = %s' % nobs)
    fixed = True
else:
    fixed = False
oberrvar = oberrstdev**2*np.ones(nobs,np.float)
pvob = np.empty((len(levob),nobs),np.float)
covlocal = np.empty((ny,nx),np.float)
covlocal_tmp = np.empty((nobs,nx*ny),np.float)
xens = np.empty((nanals,2,nx*ny),np.float)
if not use_letkf:
    obcovlocal = np.empty((nobs,nobs),np.float)
else:
    obcovlocal = None
obtimes = nc_truth.variables['t'][:]
assim_interval = obtimes[1]-obtimes[0]
assim_timesteps = int(np.round(assim_interval/models[0].dt))
print('# ntime,pverr_a,pvsprd_a,pverr_b,pvsprd_b,obinc_b,osprd_b,obinc_a,obsprd_a,omaomb/oberr,obbias_b,inflation')
if rp is not None:
    print('# random pattern: hcorr,tcorr,amp,nsamps,norm=%s,%s,%s,%s,%s' % (hcorr,tcorr,amp,rp.nsamples,rp.norm))

# initialize model clock
for nanal in range(nanals):
    models[nanal].t = obtimes[0]
    models[nanal].timesteps = assim_timesteps

# initialize relaxation to prior spread inflation factor.

if savedata is not None:
   nc = Dataset(savedata, mode='w', format='NETCDF4_CLASSIC')
   nc.r = models[0].r
   nc.f = models[0].f
   nc.U = models[0].U
   nc.L = models[0].L
   nc.H = models[0].H
   nc.nanals = nanals
   nc.hcovlocal_scale = hcovlocal_scale
   nc.vcovlocal_fact = vcovlocal_fact
   nc.oberrstdev = oberrstdev
   nc.levob = levob
   nc.g = nc_climo.g; nc.theta0 = nc_climo.theta0
   nc.nsq = models[0].nsq
   nc.tdiab = models[0].tdiab
   nc.dt = models[0].dt
   nc.diff_efold = models[0].diff_efold
   nc.diff_order = models[0].diff_order
   nc.filename_climo = filename_climo
   nc.filename_truth = filename_truth
   nc.symmetric = models[0].symmetric
   xdim = nc.createDimension('x',models[0].N)
   ydim = nc.createDimension('y',models[0].N)
   z = nc.createDimension('z',2)
   t = nc.createDimension('t',None)
   obs = nc.createDimension('obs',nobs)
   ens = nc.createDimension('ens',nanals)
   pv_t =\
   nc.createVariable('pv_t',np.float32,('t','z','y','x'),zlib=True)
   pv_b =\
   nc.createVariable('pv_b',np.float32,('t','ens','z','y','x'),zlib=True)
   pv_a =\
   nc.createVariable('pv_a',np.float32,('t','ens','z','y','x'),zlib=True)
   pv_a.units = 'K'
   pv_b.units = 'K'
   inf = nc.createVariable('inflation',np.float32,('t','z','y','x'),zlib=True)
   pv_obs = nc.createVariable('obs',np.float32,('t','obs'))
   x_obs = nc.createVariable('x_obs',np.float32,('t','obs'))
   y_obs = nc.createVariable('y_obs',np.float32,('t','obs'))
   # eady pv scaled by g/(f*theta0) so du/dz = d(pv)/dy
   xvar = nc.createVariable('x',np.float32,('x',))
   xvar.units = 'meters'
   yvar = nc.createVariable('y',np.float32,('y',))
   yvar.units = 'meters'
   zvar = nc.createVariable('z',np.float32,('z',))
   zvar.units = 'meters'
   tvar = nc.createVariable('t',np.float32,('t',))
   tvar.units = 'seconds'
   ensvar = nc.createVariable('ens',np.int32,('ens',))
   ensvar.units = 'dimensionless'
   xvar[:] = np.arange(0,models[0].L,models[0].L/models[0].N)
   yvar[:] = np.arange(0,models[0].L,models[0].L/models[0].N)
   zvar[0] = 0; zvar[1] = models[0].H
   ensvar[:] = np.arange(1,nanals+1)

kespec_errmean = None; kespec_sprdmean = None

ncount = 0; nanals2 = 4
for ntime in range(nassim):

    # check model clock
    if models[0].t != obtimes[ntime]:
        raise ValueError('model/ob time mismatch %s vs %s' %\
        (models[0].t, obtimes[ntime]))

    t1 = time.time()
    if not fixed:
        p = np.ones((ny,nx),np.float)/(nx*ny)
        #psave = p.copy()
        #p[ny/4:3*ny/4,nx/4:3*nx/4] = 4.*psave[ny/4:3*ny/4,nx/4:3*nx/4]
        #p = p - p.sum()/(nx*ny) + 1./(nx*ny)
        indxob = np.random.choice(nx*ny,nobs,replace=False,p=p.ravel())
    else:
        mask = np.zeros((ny,nx),np.bool)
        nskip = int(nx/np.sqrt(nobs))
        # if every other grid point observed, shift every other time step
        # so every grid point is observed in 2 cycle.
        if nskip == 2 and ntime%2:
            mask[1:ny:nskip,1:nx:nskip] = True
        else:
            mask[0:ny:nskip,0:nx:nskip] = True
        tmp = np.arange(0,nx*ny).reshape(ny,nx)
        indxob = tmp[mask.nonzero()].ravel()
    for k in range(len(levob)):
        # surface temp obs
        pvob[k] = scalefact*pv_truth[ntime,k,:,:].ravel()[indxob]
        pvob[k] += np.random.normal(scale=oberrstdev,size=nobs) # add ob errors
    xob = x.ravel()[indxob]
    yob = y.ravel()[indxob]
    # plot ob network
    #import matplotlib.pyplot as plt
    #plt.contourf(x,y,pv_truth[0,1,...],15)
    #plt.scatter(xob,yob,color='k')
    #plt.axis('off')
    #plt.show()
    #raise SystemExit
    # compute covariance localization function for each ob
    if not fixed or ntime == 0:
        for nob in range(nobs):
            dist = cartdist(xob[nob],yob[nob],x,y,nc_climo.L,nc_climo.L)
            covlocal = gaspcohn(dist/hcovlocal_scale)
            covlocal_tmp[nob] = covlocal.ravel()
            dist = cartdist(xob[nob],yob[nob],xob,yob,nc_climo.L,nc_climo.L)
            if not use_letkf: obcovlocal[nob] = gaspcohn(dist/hcovlocal_scale)
            # plot covariance localization
            #import matplotlib.pyplot as plt
            #plt.contourf(x,y,covlocal,15)
            #plt.show()
            #raise SystemExit

    # first-guess spread (need later to compute inflation factor)
    fsprd = ((pvens - pvens.mean(axis=0))**2).sum(axis=0)/(nanals-1)

    # compute forward operator.
    # hxens is ensemble in observation space.
    hxens = np.empty((nanals,len(levob),nobs),np.float)
    for nanal in range(nanals):
        for k in range(len(levob)):
            hxens[nanal,k,...] = scalefact*pvens[nanal,k,...].ravel()[indxob] # surface pv obs
    hxensmean_b = hxens.mean(axis=0)
    obsprd = ((hxens-hxensmean_b)**2).sum(axis=0)/(nanals-1)
    # innov stats for background
    obfits = pvob - hxensmean_b
    obfits_b = (obfits**2).mean()
    obbias_b = obfits.mean()
    obsprd_b = obsprd.mean()
    pvensmean_b = pvens.mean(axis=0).copy()
    pverr_b = (scalefact*(pvensmean_b-pv_truth[ntime]))**2
    pvsprd_b = ((scalefact*(pvensmean_b-pvens))**2).sum(axis=0)/(nanals-1)

    if savedata is not None:
        pv_t[ntime] = pv_truth[ntime]
        pv_b[ntime,:,:,:] = scalefact*pvens
        pv_obs[ntime] = pvob
        x_obs[ntime] = xob
        y_obs[ntime] = yob

    # EnKF update
    # create 1d state vector.
    for nanal in range(nanals):
        xens[nanal] = pvens[nanal].reshape((2,nx*ny))
    # update state vector.
    if direct_insertion and nobs == nx*ny and levob == [0,1]:
        for nanal in range(nanals):
            xens[nanal] =\
            pv_truth[ntime].reshape(2,nx*ny) + \
            np.random.normal(scale=oberrstdev,size=(2,nx*ny))/scalefact
        xens = xens - xens.mean(axis=0) + \
        pv_truth[ntime].reshape(2,nx*ny) + \
        np.random.normal(scale=oberrstdev,size=(2,nx*ny))/scalefact
    else:
        xens =\
        enkf_update(xens,hxens,pvob,oberrvar,covlocal_tmp,levob,vcovlocal_fact,obcovlocal=obcovlocal)
    # back to 3d state vector
    for nanal in range(nanals):
        pvens[nanal] = xens[nanal].reshape((2,ny,nx))
    t2 = time.time()
    if profile: print('cpu time for EnKF update',t2-t1)

    # forward operator on posterior ensemble.
    for nanal in range(nanals):
        for k in range(len(levob)):
            hxens[nanal,k,...] = scalefact*pvens[nanal,k,...].ravel()[indxob] # surface pv obs

    # ob space diagnostics
    hxensmean_a = hxens.mean(axis=0)
    obsprd_a = (((hxens-hxensmean_a)**2).sum(axis=0)/(nanals-1)).mean()
    # expected value is HPaHT (obsprd_a).
    obinc_a = ((hxensmean_a-hxensmean_b)*(pvob-hxensmean_a)).mean()
    # expected value is HPbHT (obsprd_b).
    obinc_b = ((hxensmean_a-hxensmean_b)*(pvob-hxensmean_b)).mean()
    # expected value R (oberrvar).
    omaomb = ((pvob-hxensmean_a)*(pvob-hxensmean_b)).mean()

    # posterior multiplicative inflation.
    pvensmean_a = pvens.mean(axis=0)
    pvprime = pvens-pvensmean_a
    asprd = (pvprime**2).sum(axis=0)/(nanals-1)
    if covinflate2 < 0:
        # relaxation to prior stdev (Whitaker & Hamill 2012)
        asprd = np.sqrt(asprd); fsprd = np.sqrt(fsprd)
        inflation_factor = 1.+covinflate1*(fsprd-asprd)/asprd
    else:
        # Hodyss et al 2016 inflation (covinflate1=covinflate2=1 works well in perfect
        # model, linear gaussian scenario)
        # inflation = asprd + (asprd/fsprd)**2((fsprd/nanals)+2*inc**2/(nanals-1))
        inc = pvensmean_a - pvensmean_b
        inflation_factor = covinflate1*asprd + \
        (asprd/fsprd)**2*((fsprd/nanals) + covinflate2*(2.*inc**2/(nanals-1)))
        inflation_factor = np.sqrt(inflation_factor/asprd)
    pvprime = pvprime*inflation_factor
    pvens = pvprime + pvensmean_a

    # print out analysis error, spread and innov stats for background
    pverr_a = (scalefact*(pvensmean_a-pv_truth[ntime]))**2
    pvsprd_a = ((scalefact*(pvensmean_a-pvens))**2).sum(axis=0)/(nanals-1)
    print("%s %g %g %g %g %g %g %g %g %g %g %g" %\
    (ntime,np.sqrt(pverr_a.mean()),np.sqrt(pvsprd_a.mean()),\
     np.sqrt(pverr_b.mean()),np.sqrt(pvsprd_b.mean()),\
     obinc_b,obsprd_b,obinc_a,obsprd_a,omaomb/oberrvar.mean(),obbias_b,inflation_factor.mean()))

    # save data.
    if savedata is not None:
        pv_a[ntime,:,:,:] = scalefact*pvens
        tvar[ntime] = obtimes[ntime]
        inf[ntime] = inflation_factor
        nc.sync()

    # run forecast ensemble to next analysis time
    t1 = time.time()
    for nanal in range(nanals):
        pvens[nanal] = models[nanal].advance(pvens[nanal])
    #for nanal in range(nanals):
    #    models[nanal].pvspec = rfft2(pvens[nanal])
    #for nt in range(assim_timesteps):
    #    for nanal in range(nanals):
    #        models[nanal].timestep()
    #for nanal in range(nanals):
    #    pvens[nanal] = irfft2(models[nanal].pvspec)
    t2 = time.time()
    if profile: print('cpu time for ens forecast',t2-t1)

    if ntime >= nassim_spinup:
        pvfcstmean = pvens.mean(axis=0)
        pverrspec = scalefact*rfft2(pvfcstmean - pv_truth[ntime+1])
        psispec = models[0].invert(pverrspec)
        psispec = psispec/(models[0].N*np.sqrt(2.))
        kespec = (models[0].ksqlsq*(psispec*np.conjugate(psispec))).real
        if kespec_errmean is None:
            kespec_errmean =\
            (models[0].ksqlsq*(psispec*np.conjugate(psispec))).real
        else:
            kespec_errmean = kespec_errmean + kespec
        for nanal in range(nanals2):
            pvsprdspec = scalefact*rfft2(pvens[nanal] - pvfcstmean)
            psispec = models[0].invert(pvsprdspec)
            psispec = psispec/(models[0].N*np.sqrt(2.))
            kespec = (models[0].ksqlsq*(psispec*np.conjugate(psispec))).real
            if kespec_sprdmean is None:
                kespec_sprdmean =\
                (models[0].ksqlsq*(psispec*np.conjugate(psispec))).real/nanals2
            else:
                kespec_sprdmean = kespec_sprdmean+kespec/nanals2
        ncount += 1

if savedata: nc.close()

kespec_sprdmean = kespec_sprdmean/ncount
kespec_errmean = kespec_errmean/ncount

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
N = models[0].N
k = np.abs((N*np.fft.fftfreq(N))[0:(N/2)+1])
l = N*np.fft.fftfreq(N)
k,l = np.meshgrid(k,l)
ktot = np.sqrt(k**2+l**2)
ktotmax = (N/2)+1
kespec_err = np.zeros(ktotmax,np.float)
kespec_sprd = np.zeros(ktotmax,np.float)
for i in range(kespec_errmean.shape[2]):
    for j in range(kespec_errmean.shape[1]):
        totwavenum = ktot[j,i]
        if int(totwavenum) < ktotmax:
            kespec_err[int(totwavenum)] = kespec_err[int(totwavenum)] +\
            kespec_errmean[:,j,i].mean(axis=0)
            kespec_sprd[int(totwavenum)] = kespec_sprd[int(totwavenum)] +\
            kespec_sprdmean[:,j,i].mean(axis=0)

print('# mean error/spread',kespec_errmean.sum(), kespec_sprdmean.sum())
plt.figure()
wavenums = np.arange(ktotmax,dtype=np.float)
for n in range(1,ktotmax):
    print('# ',wavenums[n],kespec_err[n],kespec_sprd[n])
plt.loglog(wavenums[1:-1],kespec_err[1:-1],color='r')
plt.loglog(wavenums[1:-1],kespec_sprd[1:-1],color='b')
plt.title('error (red) and spread (blue) spectra')
exptname = os.getenv('exptname','test')
plt.savefig('errorspread_spectra_%s.png' % exptname)
