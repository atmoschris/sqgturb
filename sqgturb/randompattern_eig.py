import numpy as np
from scipy.linalg import eigh
from scipy.special import gamma,kv

def _gaussian(rr,corrl):
    # gaussian covariance model.
    r = rr/corrl
    return np.exp(-r**2)

def _matern(rr,corrl,kappa=5./2.):
    # matern covariance model
    r = rr/(corrl/np.sqrt(2.))
    r = np.where(r < 1.e-10, 1.e-10, r)
    r1 = 2 ** (kappa-1.0) * gamma(kappa)
    bes = kv(kappa,r)
    return (1.0/r1) * r ** kappa * bes

def _cartdist(x1,y1,x2,y2,xmax,ymax):
    # cartesian distance on doubly periodic plane
    dx = np.abs(x1 - x2)
    dy = np.abs(y1 - y2)
    dx = np.where(dx > 0.5*xmax, xmax - dx, dx)
    dy = np.where(dy > 0.5*ymax, ymax - dy, dy)
    return np.sqrt(dx**2 + dy**2)

class RandomPatternEig:
    def __init__(self, spatial_corr_efold, temporal_corr_efold, L, N,\
                 dt, nsamples=1, stdev=1.0, thresh = 0.99, verbose=False, seed=None):
        self.hcorr = spatial_corr_efold
        self.tcorr = temporal_corr_efold
        self.dt = dt
        self.lag1corr = np.exp(-1)**(self.dt/self.tcorr)
        self.L = L
        self.stdev = stdev
        self.nsamples = nsamples
        self.N = N
        self.thresh = thresh
        # construct covariance matrix.
        x1 = np.arange(0,self.L,self.L/self.N)
        y1 = np.arange(0,self.L,self.L/self.N)
        x, y = np.meshgrid(x1, y1)
        x2 = x.flatten(); y2 = y.flatten()
        cov = np.zeros((N**2,N**2),np.float64)
        n = 0
        for x0,y0 in zip(x2,y2):
            r = _cartdist(x0,y0,x2,y2,self.L,self.L)
            cov[n,:] = _matern(r,self.hcorr)
            n = n + 1
        # eigenanalysis
        evals, evecs = eigh(cov)
        if self.thresh == 1.0:
            evals = np.where(evals > 1.e-10, evals, 1.e-10)
            self.scaledevecs = evecs*np.sqrt(evals)
            self.nevecs = self.N**2
        else:
            evalsum = evals.sum(); neig = 0; frac = 0.
            while frac < self.thresh:
                frac = evals[self.N**2-neig-1:self.N**2].sum()/evalsum
                neig += 1
            self.scaledevecs = (evecs*np.sqrt(evals/frac))[:,self.N**2-neig:self.N**2]
            if verbose:
                print '%s eigenvectors explain %s percent of variance' %\
                (neig,100*self.thresh)
            self.nevecs = neig
        # initialize random coefficients.
        if seed is None:
            self.rs = np.random.RandomState() 
        else:
            self.rs = np.random.RandomState(seed) 
        self.coeffs = self.rs.normal(size=(self.nsamples,self.nevecs))
        self.pattern = self.random_sample()

    def copy(self,seed):
        import copy
        newself = copy.copy(self)
        newself.rs = np.random.RandomState(seed)
        newself.coeffs = newself.rs.normal(size=(self.nsamples,self.nevecs))
        newself.pattern = newself.random_sample()
        return newself
     
    def random_sample(self):
        """
        return random sample
        """
        xens = np.dot(self.stdev*self.coeffs,self.scaledevecs.T)
        #xens = np.zeros((nsamples,self.N*self.N),np.float32)
        #for n in range(nsamples):
        #    for j in range(self.nevecs):
        #        xens[n] = xens[n]+self.stdev*self.coeffs[n,j]*self.scaledevecs[:,j]
        return xens.reshape((self.nsamples, self.N, self.N)).squeeze()

    def evolve(self):
        """
        evolve sample one time step
        """
        self.coeffs = \
        np.sqrt(1.-self.lag1corr**2)* \
        self.rs.normal(size=(self.nsamples,self.nevecs)) + \
        self.lag1corr*self.coeffs
        self.pattern = self.random_sample()

if __name__ == "__main__":
    import matplotlib.pyplot as plt
    import cPickle
    nsamples = 10; stdev = 2
    rp=RandomPatternEig(0.5*20.e6/64.,3600.,20.e6,64,1800,nsamples=nsamples,stdev=stdev,verbose=True)
    rp1 = rp.copy(seed=42)
    # test pickling/unpickling
    f = open('saved_rp.pickle','wb')
    cPickle.dump(rp1, f, protocol=cPickle.HIGHEST_PROTOCOL)
    f.close()
    f = open('saved_rp.pickle','rb')
    rp = cPickle.load(f)
    f.close()
    # plot random sample.
    xens = rp.pattern
    minmax = max(np.abs(xens.min()), np.abs(xens.max()))
    for n in range(nsamples):
        plt.figure()
        plt.imshow(xens[n],plt.cm.bwr,interpolation='nearest',origin='lower',vmin=-minmax,vmax=minmax)
        plt.title('pattern %s' % n)
        plt.colorbar()
    print 'variance =', ((xens**2).sum(axis=0)/(nsamples-1)).mean()
    print '(expected ',stdev**2,')'
    plt.show()
    nsamples = 1; stdev = 2
    rp = RandomPatternEig(500.e3,3600.,20.e6,64,1800,nsamples=nsamples,stdev=stdev)
    ntimes = 100
    x = rp.pattern
    lag1cov = np.zeros(x.shape, x.dtype)
    lag1var = np.zeros(x.shape, x.dtype)
    for nt in range(ntimes):
        xold = x.copy()
        rp.evolve()
        x = rp.pattern
        lag1cov = lag1cov + x*xold/(ntimes-1)
        lag1var = lag1var + x*x/(ntimes-1)
    lag1corr = lag1cov/lag1var
    print 'lag 1 autocorr = ',lag1corr.mean(), ', expected ',rp.lag1corr
    print 'variance = ',lag1var.mean()
