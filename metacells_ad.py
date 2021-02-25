import numpy as np
import pandas as pd 
import palantir
from tqdm.notebook import tqdm
from sklearn.metrics import pairwise_distances as cdist

import build_graph

class Metacells:
    """
    Fast kernel archetypal analysis.
    Finds archetypes and weights given annotated data matrix

    Attributes:
        k: number of components
        max_iter: maximum number of iterations for Frank-Wolfe update
        verbose: verbosity
    """

    def __init__(self, ad, build_kernel_on, n_metacells:int, max_iter:int=50, verbose:bool=True, true_B=None, true_A=None):
        self.ad = ad
        if build_kernel_on not in ad.obsm:
            raise ValueError(f'Key {build_kernel_on} is not present in AnnData obsm.')
        self.build_kernel_on = build_kernel_on
        self.k = n_metacells
        self.max_iter = max_iter
        self.verbose = verbose
        self.true_B = true_B
        self.true_A = true_A
    
    def _updateA(self, A, B):
        """
        Given archetype matrix B and kernel matrix K, compute assignment matrix A

        Inputs:
            K: n*n kernel matrix (sparse)
            B: n*k matrix (dense)

        Returns:
            A: k*n matrix (dense)
        """
        n, k = B.shape

        # initialize matrix A (don't reinitialize?)
        A = np.zeros((k, n))
        A[0,:] = 1.

        t = 0 # current iteration (determine multiplicative update)

        # precompute some gradient terms
        t2 = (self.K @ B).T
        t1 = t2 @ B

        # update rows of A for given number of iterations
        while t < self.max_iter:

            # compute gradient (must convert matrix to ndarray)
            G = 2. * np.array(t1 @ A - t2)

            # get argmins
            amins = np.argmin(G, axis=0)

            # loop free implementation
            e = np.zeros((k,n))
            e[amins, np.arange(n)] = 1.

            A += 2. / (t + 2.) * (e - A)
            t += 1

        return A

    def _updateB(self, A, B):
        """Given assignment matrix A and kernel matrix K, compute archetype matrix B

        Inputs:
            K: n*n kernel matrix (sparse)
            A: k*n matrix (dense)

        Returns:
            B: n*k matrix (dense)
        """
        K = self.K
        k, n = A.shape

        # initialize matrix B (don't re-initialize?)
        B = np.zeros((n,k))
        B[0, :] = 1.

        # keep track of error
        t = 0

        # precompute some terms
        t1 = A @ A.T
        t2 = K @ A.T

        # update rows of B for a given number of iterations
        while t < self.max_iter:

            # compute gradient (need to convert np.matrix to np.array)
            G = 2. * np.array(K @ B @ t1 - t2)

            # get all argmins
            amins = np.argmin(G, axis=0)

            e = np.zeros((n,k))
            e[amins, np.arange(k)] = 1.

            B += 2. / (t+2.) * (e - B)

            t += 1

        return B
    
    def _waypoint_initialize_archetypes(self):
        """
        
        Inputs:
            ad - AnnData object containing data to build diffusion components
        """
        
        k = self.k
        ad = self.ad
        
        if "X_pca" in ad.obsm:
            pca_components = pd.DataFrame(ad.obsm['X_pca']).set_index(ad.obs_names)
        else:
            # Compute PCA components from ad object
            pca_components, _ = palantir.utils.run_pca(ad, use_hvg=False)
            
        print(pca_components.shape)
        if self.verbose:
            print('Computing diffusion components for waypoint initialization ... ')
            
        dm_res = palantir.utils.run_diffusion_maps(pca_components)
        dc_components = palantir.utils.determine_multiscale_space(dm_res, n_eigs=8)
        if self.verbose:
            print('Done.')

        # Initialize metacells via waypoint sampling 
        if self.verbose:
            print('Sampling waypoints ...')
        waypoint_init = palantir.core._max_min_sampling(data=dc_components, num_waypoints=k)
        dc_components['iix'] = np.arange(len(dc_components))
        waypt_ix = dc_components.loc[waypoint_init]['iix'].values
        if self.verbose:
            print('Done.')
        
        B0 = np.zeros((len(pca_components), k))
        idx1 = list(zip(waypt_ix,np.arange(k)))
        B0[tuple(zip(*idx1))] = 1
            
        return B0 
    
    def _greedy_initialize_archetypes(self):
        """Fast greedy adaptive CSSP

        From https://arxiv.org/pdf/1312.6838.pdf


        Inputs:
            K (n*n) kernel matrix
        """
        
        K = self.K
        n = K.shape[0]
        k = self.k
        
        X=K

        if self.verbose:
            print("Initializing residual matrix using greedy column selection")

        # precompute A.T * A
        #ATA = K.T @ K
        ATA = K

        if self.verbose:
            print("Initializing f and g...")

        f = np.array((ATA.multiply(ATA)).sum(axis=0)).ravel()
        #f = np.array((ATA * ATA).sum(axis=0)).ravel()
        g = np.array(ATA.diagonal()).ravel()

        d = np.zeros((k, n))
        omega = np.zeros((k, n))

        # keep track of selected indices
        centers = np.zeros(k, dtype=int)

        # sampling
        for j in tqdm(range(k)):

            score = f/g
            p = np.argmax(score)

            # print residuals
            residual = np.sum(f)

            delta_term1 = ATA[:,p].toarray().squeeze()
            #print(delta_term1)
            delta_term2 = np.multiply(omega[:,p].reshape(-1,1), omega).sum(axis=0).squeeze()
            delta = delta_term1 - delta_term2

            # some weird rounding errors
            delta[p] = np.max([0, delta[p]])

            o = delta / np.max([np.sqrt(delta[p]), 1e-6])
            omega_square_norm = np.linalg.norm(o)**2
            omega_hadamard = np.multiply(o, o)
            term1 = omega_square_norm * omega_hadamard

            # update f (term2)
            pl = np.zeros(n)
            for r in range(j):
                omega_r = omega[r,:]
                pl += np.dot(omega_r, o) * omega_r

            ATAo = (ATA @ o.reshape(-1,1)).ravel()
            term2 = np.multiply(o, ATAo - pl)

            # update f
            f += -2. * term2 + term1

            # update g
            g += omega_hadamard

            # store omega and delta
            d[j,:] = delta
            omega[j,:] = o

            # add index
            centers[j] = int(p)

        # This is where we convert the assignment matrix B into hard assignments, I think? 
        # NO, this is just the initialization step... 
        
        B = np.zeros((n, k))
        B[centers, np.arange(k)] = 1.

        return B
    
    def _residuals(self, A, B):
        """Use trace trick to compute residual squared error
        (only works for Jaccard metric)

        Actual formula for the error is
            E = ||X - XBA||^2
            = tr(X.T @ X 
                - 2 X.T * X * B * A 
                + A.T * B.T * X.T * X * B * A)

        (Trace distributes over sums and invariant to permutation)

        Inputs:
            K: n*n kernel matrix (sparse)
            A: k*n assignments matrix (dense)
            B: n*k archetype matrix (dense)

        Returns:
            E (float)
        """
        K = self.K
        
        # term1 = np.trace(K)
        term1 = K.shape[0]
        term2 = np.trace(np.array(A @ K @ B))
        term3 = np.trace(np.array(A @ K @ A.T) @ (B.T @ B))

        return term1 - 2. * term2 + term3

    def _fit(self, n_iter:int=50, initialize_waypoint:bool=True, B0=None):
        """Compute archetypes and loadings given kernel matrix K

        Input:
            K: positive semidefinite kernel matrix (n*n)
            n_iter: number of iterations

        Updates model to add B and A
        """
        if self.verbose:
            print('Building kernel...')
        
        # input to graph construction is PCA/SVD
        kernel_model = build_graph.MetacellGraph(self.ad.obsm[self.build_kernel_on], verbose=True)

        # K is a sparse matrix representing input to metacell alg
        K = kernel_model.rbf()
        self.K = K
        
        # initialize B (update this to allow initialization from RRQR)
        n = K.shape[0]
        k = self.k

        if self.true_B is None:
            if B0 is not None:
                if self.verbose:
                    print('Using provided initial B matrix')
                B = B0
                self.B0 = B0
            else:
                if initialize_waypoint:
                    B = self._waypoint_initialize_archetypes()
                else:
                    B = self._greedy_initialize_archetypes()
                self.B0 = B 
        else:
            if self.verbose:
                print('Using fixed B matrix as provided.') 
            B = self.true_B
            
        A = np.eye(k, n)
        A[0,:] = 1.

        for it in range(n_iter):
            print("Starting iteration %d of %d" % (it+1, n_iter))
            if self.true_A is None:
                A = self._updateA(A, B)
            else:
                print('Not updating A, true A provided')
                A = self.true_A
                
            if self.true_B is None:
                B = self._updateB(A, B)
            else:
                print('Not updating B, true B provided')


            print("Completed iteration %d of %d." % (it+1, n_iter,))


        self.A_ = A
        self.B_ = B
        self.Z_ = B.T @ self.K
        
        labels = self.get_labels()
        self.ad.obs['Metacell'] = labels['metacell_ID']
        
        return 
        
    def fit(self, n_iter:int=8, initialize_waypoint:bool=True, B0=None):
            """Wrapper to fit model given kernel matrix and max number of iterations

            Inputs:
                K: kernel matrix
                n_iter (int): number of optimization iterations
                B0: initialization for B

            Returns:
                self
            """
            self._fit(n_iter, initialize_waypoint=initialize_waypoint, B0=B0)
            return self
        
    def fit_transform(self, n_iter:int=10):
        """Fit model and return archetype assignments
        """
        self._fit(n_iter)
        return self.A_

    def get_archetypes(self):
        """Return k x n matrix of archetypes"""
        return self.Z_

    def get_centers(self):
        """Return closest point to each archetype"""
        return np.argmax(self.B_, axis=0)

    def get_assignments(self):
        """Return archetype assignments for each point (n x k)
        """
        return self.A_.T

    def get_sizes(self):
        """Return size of each metacell as array
        """
        return Counter(np.argmax(self.A_, axis=0))
    
    def get_coordinates(self, X):
        """Return cluster centers"""
        # get binarized A
        A_bin = np.zeros_like(self.A_)
        A_amax = np.argmax(self.A_, axis=0)

        A_bin[A_amax, np.arange(self.A_.shape[1])] = 1

        return np.array(A_bin @ X)
    
    def binarize_matrix(self, T):
        """
        Convert matrix to binary form where the largest value in each row is 1 and all other values are 0
        """
        bin_T = np.zeros(T.shape)
        bin_T[np.argmax(T, axis=0), np.arange(T.shape[1])]=1 
        return bin_T.astype(int)

    def get_labels(self):
        bin_A = self.binarize_matrix(self.A_)
        bin_B = self.binarize_matrix(self.B_) 
       
        labels = np.dot(bin_A.T,np.arange(bin_A.shape[0])) 
   
        df = pd.DataFrame({'metacell_ID':labels.astype(int), 'is_MC':self.B_.sum(1).astype(bool)})
        df.index = self.ad.obs_names
        
        di = df[df['is_MC']==True]['metacell_ID'].reset_index().set_index('metacell_ID').to_dict()['index']

        df['metacell_ID'] = df['metacell_ID'].map(di) 
        
        return pd.DataFrame(df['metacell_ID'])
    
    