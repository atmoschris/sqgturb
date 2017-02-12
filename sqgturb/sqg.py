from __future__ import print_function
import os
import numpy as np
from scipy import ndimage

try: # pyfftw is *much* faster
    from pyfftw.interfaces import numpy_fft, cache
    #print('# using pyfftw...')
    cache.enable()
    rfft2 = numpy_fft.rfft2; irfft2 = numpy_fft.irfft2
except ImportError: # fall back on numpy fft.
    print('# WARNING: using numpy fft (install pyfftw for better performance)...')
    def rfft2(*args, **kwargs):
        kwargs.pop('threads',None)
        return np.fft.rfft2(*args,**kwargs)
    def irfft2(*args, **kwargs):
        kwargs.pop('threads',None)
        return np.fft.irfft2(*args,**kwargs)

class SQG:

    def __init__(self,pv,f=1.e-4,nsq=1.e-4,L=20.e6,H=10.e3,U=30.,\
                 r=0.,tdiab=10.*86400,diff_order=8,diff_efold=None,
                 symmetric=True,dt=None,dealias=True,threads=1,precision='single'):
        # initialize SQG model.
        if pv.shape[0] != 2:
            raise ValueError('1st dim of pv should be 2')
        N = pv.shape[1] # number of grid points in each direction
        # N should be even
        if N%2:
            raise ValueError('N must be even (powers of 2 are fastest)')
        if dt is None: # time step must be specified
            raise ValueError('must specify time step')
        if diff_efold is None: # efolding time scale for diffusion must be specified
            raise ValueError('must specify efolding time scale for diffusion')
        # number of openmp threads to use for FFTs (only for pyfftw)
        self.threads = threads
        self.N = N
        if precision == 'single':
            # ffts in single precision (faster)
            dtype = np.float32
        elif precision == 'double':
            # ffts in double precision
            dtype = np.float64
        else:
            msg="precision must be 'single' or 'double'"
            raise ValueError(msg)
        # force arrays to be float32 for precision='single' (ffts are twice as fast)
        self.nsq = np.array(nsq,dtype) # Brunt-Vaisalla (buoyancy) freq squared
        self.f = np.array(f,dtype) # coriolis
        self.H = np.array(H,dtype) # height of upper boundary
        self.U = np.array(U,dtype) # basic state velocity at z = H
        self.L = np.array(L,dtype) # size of square domain.
        self.dt = np.array(dt,dtype) # time step (seconds)
        self.dealias = dealias  # if True, dealiasing applied using 2/3 rule.
        if r < 1.e-10:
            self.ekman = False
        else:
            self.ekman = True
        self.r = np.array(r,dtype) # Ekman damping (at z=0)
        self.tdiab = np.array(tdiab,dtype) # thermal relaxation damping.
        self.t = 0 # initialize time counter
        # setup basic state pv (for thermal relaxation)
        self.symmetric = symmetric # symmetric jet, or jet with U=0 at sfc.
        y = np.arange(0,L,L/N,dtype=dtype)
        pvbar = np.zeros((2,N),dtype)
        pi = np.array(np.pi,dtype)
        l = 2.*pi/L; mu = l*np.sqrt(nsq)*H/f
        if symmetric:
            # symmetric version, no difference between upper and lower
            # boundary.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = -0.5*U*np.sin(l*y)*np.sinh(mu*(z-0.5*H)/H)*np.sin(l*y)/np.sinh(0.5*mu)
            # theta = (f*theta0/g)*(0.5*U*mu/(l*H))*np.cosh(mu*(z-0.5*H)/H)*np.cos(l*y)/np.sinh(0.5*mu) + \
            #         theta0 + (theta0*nsq*z/g)
            pvbar[:] = -(mu*0.5*U/(l*H))*np.cosh(0.5*mu)*np.cos(l*y)/np.sinh(0.5*mu)
        else:
            # asymmetric version, equilibrium state has no flow at surface and
            # temp gradient slightly weaker at sfc.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = U*np.sin(l*y)*np.sinh(mu*z/H)*np.sin(l*y)/np.sinh(mu)
            # theta = (f*theta0/g)*(U*mu/(l*H))*np.cosh(mu*z/H)*np.cos(l*y)/np.sinh(mu) +\
            # theta0 + (theta0*nsq*z/g)
            pvbar[:]   = -(mu*U/(l*H))*np.cos(l*y)/np.sinh(mu)
            pvbar[1,:] = pvbar[0,:]*np.cosh(mu)
        pvbar.shape = (2,N,1)
        pvbar = pvbar*np.ones((2,N,N),dtype)
        self.pvbar = pvbar
        self.pvspec_eq = rfft2(pvbar) # state to relax to with timescale tdiab
        self.pvspec = rfft2(pv) # initial pv field (spectral)
        # spectral stuff
        k = (N*np.fft.fftfreq(N))[0:(N/2)+1]
        l = N*np.fft.fftfreq(N)
        k,l = np.meshgrid(k,l)
        k = k.astype(dtype); l = l.astype(dtype)
        # dimensionalize wavenumbers.
        k = 2.*pi*k/self.L; l = 2.*pi*l/self.L
        ksqlsq = k**2+l**2
        self.k = k; self.l = l; self.ksqlsq = ksqlsq
        self.ik = (1.j*k).astype(np.complex64)
        self.il = (1.j*l).astype(np.complex64)
        if dealias: # arrays needed for dealiasing nonlinear Jacobian
            k_pad = ((3*N/2)*np.fft.fftfreq(3*N/2))[0:(3*N/4)+1]
            l_pad = (3*N/2)*np.fft.fftfreq(3*N/2)
            k_pad,l_pad = np.meshgrid(k_pad,l_pad)
            k_pad = k_pad.astype(dtype); l_pad = l_pad.astype(dtype)
            k_pad = 2.*pi*k_pad/self.L; l_pad = 2.*pi*l_pad/self.L
            self.ik_pad = (1.j*k_pad).astype(np.complex64)
            self.il_pad = (1.j*l_pad).astype(np.complex64)
        mu = np.sqrt(ksqlsq)*np.sqrt(self.nsq)*self.H/self.f
        mu = mu.clip(np.finfo(mu.dtype).eps) # clip to avoid NaN
        self.Hovermu = self.H/mu
        mu = mu.astype(np.float64) # cast to avoid overflow in sinh
        self.tanhmu = np.tanh(mu).astype(dtype) # cast back to original type
        self.sinhmu = np.sinh(mu).astype(dtype)
        self.diff_order = np.array(diff_order,dtype) # hyperdiffusion order
        self.diff_efold = np.array(diff_efold,dtype) # hyperdiff time scale
        ktot = np.sqrt(ksqlsq)
        ktotcutoff = np.array(pi*N/self.L, dtype)
        # integrating factor for hyperdiffusion
        # with efolding time scale for diffusion of shortest wave (N/2)
        self.hyperdiff =\
        np.exp((-self.dt/self.diff_efold)*(ktot/ktotcutoff)**self.diff_order)
        # number of timesteps to advance each call to 'advance' method.
        self.timesteps = 1

    def invert(self,pvspec=None):
        if pvspec is None: pvspec = self.pvspec
        # invert boundary pv to get streamfunction
        psispec = np.empty((2,self.N,self.N/2+1),dtype=pvspec.dtype)
        psispec[0] = self.Hovermu*((pvspec[1]/self.sinhmu) -\
                                   (pvspec[0]/self.tanhmu))
        psispec[1] = self.Hovermu*((pvspec[1]/self.tanhmu) -\
                                   (pvspec[0]/self.sinhmu))
        return psispec

    def advance(self,pv=None):
        # given total pv on grid, advance forward
        # number of timesteps given by 'timesteps' instance var.
        # if pv not specified, use pvspec instance variable.
        if pv is not None:
            self.pvspec = rfft2(pv,threads=self.threads)
        for n in range(self.timesteps):
            self.timestep()
        return irfft2(self.pvspec,threads=self.threads)

    def specpad(self, specarr):
        # pad spectral arrays with zeros to get
        # interpolation to 3/2 larger grid using inverse fft.
        # take care of normalization factor for inverse transform.
        specarr_pad = np.zeros((2, 3*self.N/2, 3*self.N/4+1), specarr.dtype)
        specarr_pad[:,0:self.N/2,0:self.N/2] = 2.25*specarr[:,0:self.N/2,0:self.N/2]
        specarr_pad[:,-self.N/2:,0:self.N/2] = 2.25*specarr[:,-self.N/2:,0:self.N/2]
        # include negative Nyquist frequency.
        specarr_pad[:,0:self.N/2,self.N/2]=np.conjugate(2.25*specarr[:,0:self.N/2,-1])
        specarr_pad[:,-self.N/2:,self.N/2]=np.conjugate(2.25*specarr[:,-self.N/2:,-1])
        return specarr_pad

    def spectrunc(self, specarr):
        # truncate spectral array using 2/3 rule.
        specarr_trunc = np.zeros((2, self.N, self.N/2+1), specarr.dtype)
        specarr_trunc[:,0:self.N/2,0:self.N/2] = specarr[:,0:self.N/2,0:self.N/2]
        specarr_trunc[:,-self.N/2:,0:self.N/2] = specarr[:,-self.N/2:,0:self.N/2]
        return specarr_trunc

    def gettend(self,pvspec=None):
        # compute tendencies of pv on z=0,H
        # invert pv to get streamfunction
        if pvspec is None:
            pvspec = self.pvspec
        psispec = self.invert(pvspec)
        # nonlinear jacobian and thermal relaxation
        if not self.dealias:
            u = irfft2(-self.il*psispec,threads=self.threads)
            v = irfft2(self.ik*psispec,threads=self.threads)
            pvx = irfft2(self.ik*pvspec,threads=self.threads)
            pvy = irfft2(self.il*pvspec,threads=self.threads)
        else: # pad spectral coeffs with zeros for dealiased jacobian
            psispec_pad = self.specpad(psispec)
            pvspec_pad  = self.specpad(pvspec)
            u = irfft2(-self.il_pad*psispec_pad,threads=self.threads)
            v = irfft2(self.ik_pad*psispec_pad,threads=self.threads)
            pvx = irfft2(self.ik_pad*pvspec_pad,threads=self.threads)
            pvy = irfft2(self.il_pad*pvspec_pad,threads=self.threads)
        advection = u*pvx + v*pvy
        jacobianspec = rfft2(advection,threads=self.threads)
        if self.dealias: # 2/3 rule: truncate spectral coefficients of jacobian
            jacobianspec = self.spectrunc(jacobianspec)
        dpvspecdt = (1./self.tdiab)*(self.pvspec_eq-pvspec)-jacobianspec
        # Ekman damping at boundaries.
        if self.ekman:
            dpvspecdt[0] += self.r*self.ksqlsq*psispec[0]
            # for asymmetric jet (U=0 at sfc), no Ekman layer at lid
            if self.symmetric:
                dpvspecdt[1] -= self.r*self.ksqlsq*psispec[1]
        # save wind field
        self.u = u; self.v = v
        return dpvspecdt

    def timestep(self):
        # update pv using 4th order runge-kutta time step with
        # implicit "integrating factor" treatment of hyperdiffusion.
        k1 = self.dt*self.gettend(self.pvspec)
        k2 = self.dt*self.gettend(self.pvspec + 0.5*k1)
        k3 = self.dt*self.gettend(self.pvspec + 0.5*k2)
        k4 = self.dt*self.gettend(self.pvspec + k3)
        self.pvspec = self.hyperdiff*(self.pvspec + (k1+2.*k2+2.*k3+k4)/6.)
        self.t += self.dt # increment time

class SQGpert:

    def __init__(self,pv,f=1.e-4,nsq=1.e-4,L=20.e6,H=10.e3,U=30.,
                 r=0.,tdiab=10.*86400,diff_order=8,diff_efold=None,
                 diff_efold_pert=8,diff_order_pert=None,pert_shift=1.0,pert_amp=0.0,pert_corr=1.,
                 windpert_max=1.e30,
                 symmetric=True,dt=None,dealias=True,threads=1,precision='single'):
        # initialize SQG model.
        if pv.shape[0] != 2:
            raise ValueError('1st dim of pv should be 2')
        N = pv.shape[1] # number of grid points in each direction
        # N should be even
        if N%2:
            raise ValueError('N must be even (powers of 2 are fastest)')
        if dt is None: # time step must be specified
            raise ValueError('must specify time step')
        if diff_efold is None: # efolding time scale for diffusion must be specified
            raise ValueError('must specify efolding time scale for diffusion')
        # number of openmp threads to use for FFTs (only for pyfftw)
        self.threads = threads
        self.N = N
        if precision == 'single':
            # ffts in single precision (faster)
            dtype = np.float32
        elif precision == 'double':
            # ffts in double precision
            dtype = np.float64
        else:
            msg="precision must be 'single' or 'double'"
            raise ValueError(msg)
        # force arrays to be float32 for precision='single' (ffts are twice as fast)
        self.nsq = np.array(nsq,dtype) # Brunt-Vaisalla (buoyancy) freq squared
        self.f = np.array(f,dtype) # coriolis
        self.H = np.array(H,dtype) # height of upper boundary
        self.U = np.array(U,dtype) # basic state velocity at z = H
        self.L = np.array(L,dtype) # size of square domain.
        self.dt = np.array(dt,dtype) # time step (seconds)
        self.dealias = dealias  # if True, dealiasing applied using 2/3 rule.
        if r < 1.e-10:
            self.ekman = False
        else:
            self.ekman = True
        self.r = np.array(r,dtype) # Ekman damping (at z=0)
        self.tdiab = np.array(tdiab,dtype) # thermal relaxation damping.
        self.t = 0 # initialize time counter
        # setup basic state pv (for thermal relaxation)
        self.symmetric = symmetric # symmetric jet, or jet with U=0 at sfc.
        y = np.arange(0,L,L/N,dtype=dtype)
        pvbar = np.zeros((2,N),dtype)
        pi = np.array(np.pi,dtype)
        l = 2.*pi/L; mu = l*np.sqrt(nsq)*H/f
        if symmetric:
            # symmetric version, no difference between upper and lower
            # boundary.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = -0.5*U*np.sin(l*y)*np.sinh(mu*(z-0.5*H)/H)*np.sin(l*y)/np.sinh(0.5*mu)
            # theta = (f*theta0/g)*(0.5*U*mu/(l*H))*np.cosh(mu*(z-0.5*H)/H)*np.cos(l*y)/np.sinh(0.5*mu) + \
            #         theta0 + (theta0*nsq*z/g)
            pvbar[:] = -(mu*0.5*U/(l*H))*np.cosh(0.5*mu)*np.cos(l*y)/np.sinh(0.5*mu)
        else:
            # asymmetric version, equilibrium state has no flow at surface and
            # temp gradient slightly weaker at sfc.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = U*np.sin(l*y)*np.sinh(mu*z/H)*np.sin(l*y)/np.sinh(mu)
            # theta = (f*theta0/g)*(U*mu/(l*H))*np.cosh(mu*z/H)*np.cos(l*y)/np.sinh(mu) +\
            # theta0 + (theta0*nsq*z/g)
            pvbar[:]   = -(mu*U/(l*H))*np.cos(l*y)/np.sinh(mu)
            pvbar[1,:] = pvbar[0,:]*np.cosh(mu)
        pvbar.shape = (2,N,1)
        pvbar = pvbar*np.ones((2,N,N),dtype)
        self.pvbar = pvbar
        self.pvspec_eq = rfft2(pvbar) # state to relax to with timescale tdiab
        self.pvspec = rfft2(pv) # initial pv field (spectral)
        # spectral stuff
        k = (N*np.fft.fftfreq(N))[0:(N/2)+1]
        l = N*np.fft.fftfreq(N)
        k,l = np.meshgrid(k,l)
        k = k.astype(dtype); l = l.astype(dtype)
        # dimensionalize wavenumbers.
        k = 2.*pi*k/self.L; l = 2.*pi*l/self.L
        ksqlsq = k**2+l**2
        self.k = k; self.l = l; self.ksqlsq = ksqlsq
        self.ik = (1.j*k).astype(np.complex64)
        self.il = (1.j*l).astype(np.complex64)
        if dealias: # arrays needed for dealiasing nonlinear Jacobian
            k_pad = ((3*N/2)*np.fft.fftfreq(3*N/2))[0:(3*N/4)+1]
            l_pad = (3*N/2)*np.fft.fftfreq(3*N/2)
            k_pad,l_pad = np.meshgrid(k_pad,l_pad)
            k_pad = k_pad.astype(dtype); l_pad = l_pad.astype(dtype)
            k_pad = 2.*pi*k_pad/self.L; l_pad = 2.*pi*l_pad/self.L
            self.ik_pad = (1.j*k_pad).astype(np.complex64)
            self.il_pad = (1.j*l_pad).astype(np.complex64)
        mu = np.sqrt(ksqlsq)*np.sqrt(self.nsq)*self.H/self.f
        mu = mu.clip(np.finfo(mu.dtype).eps) # clip to avoid NaN
        self.Hovermu = self.H/mu
        mu = mu.astype(np.float64) # cast to avoid overflow in sinh
        self.tanhmu = np.tanh(mu).astype(dtype) # cast back to original type
        self.sinhmu = np.sinh(mu).astype(dtype)
        self.diff_order = np.array(diff_order,dtype) # hyperdiffusion order
        self.diff_efold = np.array(diff_efold,dtype) # hyperdiff time scale
        ktot = np.sqrt(ksqlsq)
        ktotcutoff = np.array(pi*N/self.L, dtype)
        # integrating factor for hyperdiffusion
        # with efolding time scale for diffusion of shortest wave (N/2)
        self.hyperdiff =\
        np.exp((-self.dt/self.diff_efold)*(ktot/ktotcutoff)**self.diff_order)
        self.diff_order_pert = diff_order_pert
        self.diff_efold_pert = diff_efold_pert
        self.pert_shift = pert_shift
        self.pert_amp = pert_amp
        self.pert_shift_prevx = np.random.normal(loc=0,scale=pert_shift)
        self.pert_shift_prevy = np.random.normal(loc=0,scale=pert_shift)
        # lag one autocorrelation of noise parameters.
        if pert_corr == 0:
            self.pert_corr = 0.
        else:
            self.pert_corr = np.exp(-1)**(1./pert_corr)
        if diff_order_pert is not None and diff_efold_pert is not None:
            self.hyperdiff_pert =\
            np.exp((-self.dt/self.diff_efold_pert)*(ktot/ktotcutoff)**self.diff_order_pert)
        else:
            self.hyperdiff_pert = 1.0

        # number of timesteps to advance each call to 'advance' method.
        self.timesteps = 1

    def invert(self,pvspec=None):
        if pvspec is None: pvspec = self.pvspec
        # invert boundary pv to get streamfunction
        psispec = np.empty((2,self.N,self.N/2+1),dtype=pvspec.dtype)
        psispec[0] = self.Hovermu*((pvspec[1]/self.sinhmu) -\
                                   (pvspec[0]/self.tanhmu))
        psispec[1] = self.Hovermu*((pvspec[1]/self.tanhmu) -\
                                   (pvspec[0]/self.sinhmu))
        return psispec

    def advance(self,pv=None):
        # given total pv on grid, advance forward
        # number of timesteps given by 'timesteps' instance var.
        # if pv not specified, use pvspec instance variable.
        if pv is not None:
            self.pvspec = rfft2(pv,threads=self.threads)
        for n in range(self.timesteps):
            self.timestep()
        return irfft2(self.pvspec,threads=self.threads)

    def specpad(self, specarr):
        # pad spectral arrays with zeros to get
        # interpolation to 3/2 larger grid using inverse fft.
        # take care of normalization factor for inverse transform.
        specarr_pad = np.zeros((2, 3*self.N/2, 3*self.N/4+1), specarr.dtype)
        specarr_pad[:,0:self.N/2,0:self.N/2] = 2.25*specarr[:,0:self.N/2,0:self.N/2]
        specarr_pad[:,-self.N/2:,0:self.N/2] = 2.25*specarr[:,-self.N/2:,0:self.N/2]
        # include negative Nyquist frequency.
        specarr_pad[:,0:self.N/2,self.N/2]=np.conjugate(2.25*specarr[:,0:self.N/2,-1])
        specarr_pad[:,-self.N/2:,self.N/2]=np.conjugate(2.25*specarr[:,-self.N/2:,-1])
        return specarr_pad

    def spectrunc(self, specarr):
        # truncate spectral array using 2/3 rule.
        specarr_trunc = np.zeros((2, self.N, self.N/2+1), specarr.dtype)
        specarr_trunc[:,0:self.N/2,0:self.N/2] = specarr[:,0:self.N/2,0:self.N/2]
        specarr_trunc[:,-self.N/2:,0:self.N/2] = specarr[:,-self.N/2:,0:self.N/2]
        return specarr_trunc

    def gettend(self,pvspec=None):
        # compute tendencies of pv on z=0,H
        # invert pv to get streamfunction
        if pvspec is None:
            pvspec = self.pvspec
        psispec = self.invert(pvspec)
        # nonlinear jacobian and thermal relaxation
        if not self.dealias:
            u = irfft2(-self.il*psispec,threads=self.threads)
            v = irfft2(self.ik*psispec,threads=self.threads)
            pvx = irfft2(self.ik*pvspec,threads=self.threads)
            pvy = irfft2(self.il*pvspec,threads=self.threads)
        else: # pad spectral coeffs with zeros for dealiased jacobian
            psispec_pad = self.specpad(psispec)
            pvspec_pad  = self.specpad(pvspec)
            u = irfft2(-self.il_pad*psispec_pad,threads=self.threads)
            v = irfft2(self.ik_pad*psispec_pad,threads=self.threads)
            pvx = irfft2(self.ik_pad*pvspec_pad,threads=self.threads)
            pvy = irfft2(self.il_pad*pvspec_pad,threads=self.threads)
        if self.pert_amp > 0:
            if self.rkfirst:
                pert_shift_currentx =\
                np.sqrt(1.-self.pert_corr**2)*np.random.normal(loc=0,scale=self.pert_shift) +\
                self.pert_corr*self.pert_shift_prevx
                pert_shift_currenty =\
                np.sqrt(1.-self.pert_corr**2)*np.random.normal(loc=0,scale=self.pert_shift) +\
                self.pert_corr*self.pert_shift_prevy
                if self.pert_shift > 0:
                    pv = irfft2(pvspec)
                    # shift pv randomly in x,y direction
                    pvshift = ndimage.shift(pv,\
                              (0,pert_shift_currentx,pert_shift_currenty), order=1, mode='wrap')
                    pvshiftspec = rfft2(pvshift)
                    # high-pass filter
                    pvpertspec = pvshiftspec - self.hyperdiff_pert*pvshiftspec
                else:
                    pvpertspec = pvspec - self.hyperdiff_pert*pvspec
                psispec_pert = self.invert(pvpertspec)
                if not self.dealias:
                    self.upert = irfft2(-self.il*psispec_pert,threads=self.threads)
                    self.vpert = irfft2(self.ik*psispec_pert,threads=self.threads)
                else: # pad spectral coeffs with zeros for dealiased jacobian
                    psispec_pad = self.specpad(psispec_pert)
                    self.upert = irfft2(-self.il_pad*psispec_pad,threads=self.threads)
                    self.vpert = irfft2(self.ik_pad*psispec_pad,threads=self.threads)
                self.upert = self.pert_amp*self.upert
                self.vpert = self.pert_amp*self.vpert
                #import matplotlib.pyplot as plt
                #im = plt.imshow(self.upert[1],cmap=plt.cm.bwr,interpolation='nearest',origin='lower',vmin=-60,vmax=60)
                #plt.figure()
                #im = plt.imshow(self.vpert[1],cmap=plt.cm.bwr,interpolation='nearest',origin='lower',vmin=-60,vmax=60)
                #plt.show()
                #raise SystemExit
                self.pert_shift_prevx  = pert_shift_currentx
                self.pert_shift_prevy  = pert_shift_currenty
                #spd = np.sqrt(self.upert**2+self.vpert**2)
                #spd2 = np.sqrt(u**2+v**2)
                #print('max pert spd',spd.max(),spd2.max())
            u += self.upert
            v += self.vpert
        advection = u*pvx + v*pvy
        jacobianspec = rfft2(advection,threads=self.threads)
        if self.dealias: # 2/3 rule: truncate spectral coefficients of jacobian
            jacobianspec = self.spectrunc(jacobianspec)
        dpvspecdt = (1./self.tdiab)*(self.pvspec_eq-pvspec)-jacobianspec
        # Ekman damping at boundaries.
        if self.ekman:
            dpvspecdt[0] += self.r*self.ksqlsq*psispec[0]
            # for asymmetric jet (U=0 at sfc), no Ekman layer at lid
            if self.symmetric:
                dpvspecdt[1] -= self.r*self.ksqlsq*psispec[1]
        # save wind field
        self.u = u; self.v = v
        return dpvspecdt

    def timestep(self):
        # update pv using 4th order runge-kutta time step with
        # implicit "integrating factor" treatment of hyperdiffusion.
        self.rkfirst = True
        k1 = self.dt*self.gettend(self.pvspec)
        self.rkfirst = False
        k2 = self.dt*self.gettend(self.pvspec + 0.5*k1)
        k3 = self.dt*self.gettend(self.pvspec + 0.5*k2)
        k4 = self.dt*self.gettend(self.pvspec + k3)
        self.pvspec = self.hyperdiff*(self.pvspec + (k1+2.*k2+2.*k3+k4)/6.)
        self.t += self.dt # increment time

class SQGrandom:

    def __init__(self,pv,f=1.e-4,nsq=1.e-4,L=20.e6,H=10.e3,U=30.,
                 r=0.,tdiab=10.*86400,diff_order=8,diff_efold=None,
                 random_pattern=None,
                 symmetric=True,dt=None,dealias=True,threads=1,precision='single'):
        # initialize SQG model.
        if pv.shape[0] != 2:
            raise ValueError('1st dim of pv should be 2')
        N = pv.shape[1] # number of grid points in each direction
        # N should be even
        if N%2:
            raise ValueError('N must be even (powers of 2 are fastest)')
        if dt is None: # time step must be specified
            raise ValueError('must specify time step')
        if diff_efold is None: # efolding time scale for diffusion must be specified
            raise ValueError('must specify efolding time scale for diffusion')
        # number of openmp threads to use for FFTs (only for pyfftw)
        self.threads = threads
        self.N = N
        if precision == 'single':
            # ffts in single precision (faster)
            dtype = np.float32
        elif precision == 'double':
            # ffts in double precision
            dtype = np.float64
        else:
            msg="precision must be 'single' or 'double'"
            raise ValueError(msg)
        # force arrays to be float32 for precision='single' (ffts are twice as fast)
        self.nsq = np.array(nsq,dtype) # Brunt-Vaisalla (buoyancy) freq squared
        self.f = np.array(f,dtype) # coriolis
        self.H = np.array(H,dtype) # height of upper boundary
        self.U = np.array(U,dtype) # basic state velocity at z = H
        self.L = np.array(L,dtype) # size of square domain.
        self.dt = np.array(dt,dtype) # time step (seconds)
        self.dealias = dealias  # if True, dealiasing applied using 2/3 rule.
        if r < 1.e-10:
            self.ekman = False
        else:
            self.ekman = True
        self.r = np.array(r,dtype) # Ekman damping (at z=0)
        self.tdiab = np.array(tdiab,dtype) # thermal relaxation damping.
        self.t = 0 # initialize time counter
        # setup basic state pv (for thermal relaxation)
        self.symmetric = symmetric # symmetric jet, or jet with U=0 at sfc.
        y = np.arange(0,L,L/N,dtype=dtype)
        pvbar = np.zeros((2,N),dtype)
        pi = np.array(np.pi,dtype)
        l = 2.*pi/L; mu = l*np.sqrt(nsq)*H/f
        if symmetric:
            # symmetric version, no difference between upper and lower
            # boundary.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = -0.5*U*np.sin(l*y)*np.sinh(mu*(z-0.5*H)/H)*np.sin(l*y)/np.sinh(0.5*mu)
            # theta = (f*theta0/g)*(0.5*U*mu/(l*H))*np.cosh(mu*(z-0.5*H)/H)*np.cos(l*y)/np.sinh(0.5*mu) + \
            #         theta0 + (theta0*nsq*z/g)
            pvbar[:] = -(mu*0.5*U/(l*H))*np.cosh(0.5*mu)*np.cos(l*y)/np.sinh(0.5*mu)
        else:
            # asymmetric version, equilibrium state has no flow at surface and
            # temp gradient slightly weaker at sfc.
            # l = 2.*pi/L and mu = l*N*H/f
            # u = U*np.sin(l*y)*np.sinh(mu*z/H)*np.sin(l*y)/np.sinh(mu)
            # theta = (f*theta0/g)*(U*mu/(l*H))*np.cosh(mu*z/H)*np.cos(l*y)/np.sinh(mu) +\
            # theta0 + (theta0*nsq*z/g)
            pvbar[:]   = -(mu*U/(l*H))*np.cos(l*y)/np.sinh(mu)
            pvbar[1,:] = pvbar[0,:]*np.cosh(mu)
        pvbar.shape = (2,N,1)
        pvbar = pvbar*np.ones((2,N,N),dtype)
        self.pvbar = pvbar
        self.pvspec_eq = rfft2(pvbar) # state to relax to with timescale tdiab
        self.pvspec = rfft2(pv) # initial pv field (spectral)
        # spectral stuff
        k = (N*np.fft.fftfreq(N))[0:(N/2)+1]
        l = N*np.fft.fftfreq(N)
        k,l = np.meshgrid(k,l)
        k = k.astype(dtype); l = l.astype(dtype)
        # dimensionalize wavenumbers.
        k = 2.*pi*k/self.L; l = 2.*pi*l/self.L
        ksqlsq = k**2+l**2
        self.k = k; self.l = l; self.ksqlsq = ksqlsq
        self.ik = (1.j*k).astype(np.complex64)
        self.il = (1.j*l).astype(np.complex64)
        if dealias: # arrays needed for dealiasing nonlinear Jacobian
            k_pad = ((3*N/2)*np.fft.fftfreq(3*N/2))[0:(3*N/4)+1]
            l_pad = (3*N/2)*np.fft.fftfreq(3*N/2)
            k_pad,l_pad = np.meshgrid(k_pad,l_pad)
            k_pad = k_pad.astype(dtype); l_pad = l_pad.astype(dtype)
            k_pad = 2.*pi*k_pad/self.L; l_pad = 2.*pi*l_pad/self.L
            self.ik_pad = (1.j*k_pad).astype(np.complex64)
            self.il_pad = (1.j*l_pad).astype(np.complex64)
        mu = np.sqrt(ksqlsq)*np.sqrt(self.nsq)*self.H/self.f
        mu = mu.clip(np.finfo(mu.dtype).eps) # clip to avoid NaN
        self.Hovermu = self.H/mu
        mu = mu.astype(np.float64) # cast to avoid overflow in sinh
        self.tanhmu = np.tanh(mu).astype(dtype) # cast back to original type
        self.sinhmu = np.sinh(mu).astype(dtype)
        self.diff_order = np.array(diff_order,dtype) # hyperdiffusion order
        self.diff_efold = np.array(diff_efold,dtype) # hyperdiff time scale
        ktot = np.sqrt(ksqlsq)
        ktotcutoff = np.array(pi*N/self.L, dtype)
        # integrating factor for hyperdiffusion
        # with efolding time scale for diffusion of shortest wave (N/2)
        self.hyperdiff =\
        np.exp((-self.dt/self.diff_efold)*(ktot/ktotcutoff)**self.diff_order)

        # number of timesteps to advance each call to 'advance' method.
        self.timesteps = 1

        # random pattern.
        self.random_pattern = random_pattern

    def invert(self,pvspec=None):
        if pvspec is None: pvspec = self.pvspec
        # invert boundary pv to get streamfunction
        psispec = np.empty((2,self.N,self.N/2+1),dtype=pvspec.dtype)
        psispec[0] = self.Hovermu*((pvspec[1]/self.sinhmu) -\
                                   (pvspec[0]/self.tanhmu))
        psispec[1] = self.Hovermu*((pvspec[1]/self.tanhmu) -\
                                   (pvspec[0]/self.sinhmu))
        return psispec

    def advance(self,pv=None):
        # given total pv on grid, advance forward
        # number of timesteps given by 'timesteps' instance var.
        # if pv not specified, use pvspec instance variable.
        if pv is not None:
            self.pvspec = rfft2(pv,threads=self.threads)
        for n in range(self.timesteps):
            self.timestep()
        return irfft2(self.pvspec,threads=self.threads)

    def specpad(self, specarr):
        # pad spectral arrays with zeros to get
        # interpolation to 3/2 larger grid using inverse fft.
        # take care of normalization factor for inverse transform.
        specarr_pad = np.zeros((2, 3*self.N/2, 3*self.N/4+1), specarr.dtype)
        specarr_pad[:,0:self.N/2,0:self.N/2] = 2.25*specarr[:,0:self.N/2,0:self.N/2]
        specarr_pad[:,-self.N/2:,0:self.N/2] = 2.25*specarr[:,-self.N/2:,0:self.N/2]
        # include negative Nyquist frequency.
        specarr_pad[:,0:self.N/2,self.N/2]=np.conjugate(2.25*specarr[:,0:self.N/2,-1])
        specarr_pad[:,-self.N/2:,self.N/2]=np.conjugate(2.25*specarr[:,-self.N/2:,-1])
        return specarr_pad

    def spectrunc(self, specarr):
        # truncate spectral array using 2/3 rule.
        specarr_trunc = np.zeros((2, self.N, self.N/2+1), specarr.dtype)
        specarr_trunc[:,0:self.N/2,0:self.N/2] = specarr[:,0:self.N/2,0:self.N/2]
        specarr_trunc[:,-self.N/2:,0:self.N/2] = specarr[:,-self.N/2:,0:self.N/2]
        return specarr_trunc

    def gettend(self,pvspec=None):
        # compute tendencies of pv on z=0,H
        # invert pv to get streamfunction
        if pvspec is None:
            pvspec = self.pvspec
        psispec = self.invert(pvspec)
        # nonlinear jacobian and thermal relaxation
        if not self.dealias:
            u = irfft2(-self.il*psispec,threads=self.threads)
            v = irfft2(self.ik*psispec,threads=self.threads)
            pvx = irfft2(self.ik*pvspec,threads=self.threads)
            pvy = irfft2(self.il*pvspec,threads=self.threads)
        else: # pad spectral coeffs with zeros for dealiased jacobian
            psispec_pad = self.specpad(psispec)
            pvspec_pad  = self.specpad(pvspec)
            u = irfft2(-self.il_pad*psispec_pad,threads=self.threads)
            v = irfft2(self.ik_pad*psispec_pad,threads=self.threads)
            pvx = irfft2(self.ik_pad*pvspec_pad,threads=self.threads)
            pvy = irfft2(self.il_pad*pvspec_pad,threads=self.threads)
        if self.random_pattern is not None:
            if self.rkfirst:
                # generate random streamfunction field,
                # then compute u,v winds
                psispec_pert = rfft2(self.random_pattern.pattern)
                if not self.dealias:
                    self.upert = irfft2(-self.il*psispec_pert,threads=self.threads)
                    self.vpert = irfft2(self.ik*psispec_pert,threads=self.threads)
                else: # pad spectral coeffs with zeros for dealiased jacobian
                    psispec_pad = self.specpad(psispec_pert)
                    self.upert = irfft2(-self.il_pad*psispec_pad,threads=self.threads)
                    self.vpert = irfft2(self.ik_pad*psispec_pad,threads=self.threads)
                # evolve random streamfunction pattern to next time step.
                self.random_pattern.evolve()
                #import matplotlib.pyplot as plt
                #im = plt.imshow(self.upert[1],cmap=plt.cm.bwr,interpolation='nearest',origin='lower')
                #spd = np.sqrt(self.upert**2+self.vpert**2)
                #spd2 = np.sqrt(u**2+v**2)
                #print('max pert spd',spd.max(),spd2.max())
                #plt.show()
                #raise SystemExit
            u += self.upert
            v += self.vpert
        advection = u*pvx + v*pvy
        jacobianspec = rfft2(advection,threads=self.threads)
        if self.dealias: # 2/3 rule: truncate spectral coefficients of jacobian
            jacobianspec = self.spectrunc(jacobianspec)
        dpvspecdt = (1./self.tdiab)*(self.pvspec_eq-pvspec)-jacobianspec
        # Ekman damping at boundaries.
        if self.ekman:
            dpvspecdt[0] += self.r*self.ksqlsq*psispec[0]
            # for asymmetric jet (U=0 at sfc), no Ekman layer at lid
            if self.symmetric:
                dpvspecdt[1] -= self.r*self.ksqlsq*psispec[1]
        # save wind field
        self.u = u; self.v = v
        return dpvspecdt

    def timestep(self):
        # update pv using 4th order runge-kutta time step with
        # implicit "integrating factor" treatment of hyperdiffusion.
        self.rkfirst = True
        k1 = self.dt*self.gettend(self.pvspec)
        self.rkfirst = False
        k2 = self.dt*self.gettend(self.pvspec + 0.5*k1)
        k3 = self.dt*self.gettend(self.pvspec + 0.5*k2)
        k4 = self.dt*self.gettend(self.pvspec + k3)
        self.pvspec = self.hyperdiff*(self.pvspec + (k1+2.*k2+2.*k3+k4)/6.)
        self.t += self.dt # increment time
