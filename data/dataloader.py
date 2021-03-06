from __future__ import print_function
import numpy as np
import pandas as pd
from itertools import product
import gc
import os, sys
data_path = os.path.join(os.environ['DEEPQSODIR'], 'data')
sys.path.insert(0, data_path)
from data_utils import *

class Dataloader(object):
    
    """
    Class equipped with functions for generating the 
    DNN training data from the input
    source tables.
    """
    
    def __init__(self, lens_source_path, nonlens_source_path, 
                 onehot_filters=False, lightcurve_only=False, observation_cutoff=np.inf, debug=False):
        self.lens_source_path = lens_source_path
        self.nonlens_source_path = nonlens_source_path
        self.lens = pd.read_csv(lens_source_path)
        self.nonlens = pd.read_csv(nonlens_source_path)
        
        self.NUM_TIMES = None # undefined until set_balance is called
        self.NUM_POSITIVES = self.lens['objectId'].nunique()
        self.NUM_FILTERS = 5
        self.seed = 123
        
        self.lightcurve_only = lightcurve_only
        if self.lightcurve_only:
            self.attributes = ['apMag', 'apMagErr', 'd_time', ]
        else:
            self.attributes = ['psf_fwhm', 'x', 'y', 'apFlux', 'apFluxErr', 
                               'apMag', 'apMagErr', 'trace', 'e1', 'e2', 'e', 'phi', 'd_time']
        self.NUM_ATTRIBUTES = len(self.attributes)
        
        self.filtered_attributes = [f + '_' + a for a, f in list(product(self.attributes, 'ugriz'))]
        assert len(self.filtered_attributes) == self.NUM_FILTERS * self.NUM_ATTRIBUTES
        
        self.onehot_filters = onehot_filters
        self.observation_cutoff = observation_cutoff
        self.DEBUG = debug
        
    def set_balance(self, lens, nonlens, observation_cutoff):
        """
        Sets the balance in numbers of objects and observations
        between lenses and nonlenses
        """
        
        # Get same number of observations, up to @observation_cutoff
        nonlens.query('MJD < @observation_cutoff', inplace=True)
        lens.query('MJD < @observation_cutoff', inplace=True)# giving up trace < 5.12
        # & objectId < (@min_nonlensid + @NUM_POSITIVES)
        self.NUM_TIMES = nonlens['MJD'].nunique()
        assert nonlens['MJD'].nunique() == lens['MJD'].nunique()
        
        # Get same number of lenses as lens sample
        final_nonlenses = nonlens['objectId'].unique()[: self.NUM_POSITIVES]
        nonlens = nonlens[nonlens['objectId'].isin(final_nonlenses)]
        gc.collect() 

        assert np.array_equal(lens.shape, nonlens.shape)
        
        return lens, nonlens
    
    def set_additional_columns(self, lens, nonlens):
        """
        Reorganizes existing columns into forms that will
        be useful in our data generation. In particular:
        (1) Converts units from e1, e2 to e, phi
        (2) Creates a column of time elapsed since last observation
        (3) Creates a column of time index (later to become prefixes to rows)
        (4) Deletes columns of MJD, ccdVisitId afterward
        """
        
        for src in [lens, nonlens]:
            # Add e, phi columns
            src['e'], src['phi'] = e1e2_to_ephi(e1=src['e1'], e2=src['e2'])
            # Set MJD relative to zero
            src['MJD'] = src['MJD'] - np.min(src['MJD'].values)
            # Map ccdVisitId to integers starting from 0
            sorted_obs_id = np.sort(src['ccdVisitId'].unique())
            time_map = dict(zip(sorted_obs_id, range(self.NUM_TIMES)))
            src['time_index'] = src['ccdVisitId'].map(time_map).astype(int)
            # Add a column of d_time, time elapsed since last observation in each filter
            src.sort_values(['objectId', 'filter', 'MJD', ], axis=0, inplace=True)
            src['d_time'] = src['MJD'] - src['MJD'].shift(+1)
            src['d_time'].fillna(0.0, inplace=True)
            src['d_time'] = np.clip(src['d_time'], a_min=0.0, a_max=None)
            src.drop(['MJD', 'ccdVisitId'], axis=1, inplace=True)
            
        gc.collect()
        
        if self.DEBUG:
            print("After set balance: ", lens.shape)
        
        return lens, nonlens
    
    def make_data_array(self, src, truth_value=1):
        """
        Performs a series of Pandas manipulations
        to get data into the shape we need, i.e.
        (@NUM_POSITIVES, @NUM_TIMES, @NUM_FILTERS).
        Refer to comments for more detail.
        """
        
        src = src[self.attributes + ['time_index', 'objectId', 'filter', ]]
        
        if self.DEBUG:
            print("NUM_TIMES: ", self.NUM_TIMES)
            print(src.shape[0])
        assert src.shape[0] == self.NUM_POSITIVES*self.NUM_TIMES
        
        if self.onehot_filters:
            # 1. One-hot encode the filter column into u, g, r, i, z
            src = pd.get_dummies(data=src, prefix='', prefix_sep='', columns=['filter'])
            #src.reset_index(inplace=True)
            gc.collect()
            if self.DEBUG:
                print("1 src shape:", src.shape)
            assert np.array_equal(src.shape, 
                                  (self.NUM_POSITIVES*self.NUM_TIMES,
                                   self.NUM_ATTRIBUTES + self.NUM_FILTERS + 2))
                                # 2 refers to objectId, time_index
                
            # 2. Pivot to get time sequence in each row
            ohfiltered_attributes = self.attributes[:] + ['u', 'g', 'r', 'i', 'z']
            src = src.pivot_table(index=['objectId'], 
                                columns=['time_index'], 
                                values=ohfiltered_attributes,
                                dropna=False)

            # 3. Collapse multi-indexed column with '-' between time and ohfiltered_attribute
            src.columns = src.columns.map('{0[1]}-{0[0]}'.format)
            timed_ohfiltered_attributes = [str(t) + '-' + a for a, t in\
                                              list(product(ohfiltered_attributes, range(self.NUM_TIMES)))]
            gc.collect()
            assert np.array_equal(src.shape, 
                                  (self.NUM_POSITIVES, 
                                   (self.NUM_ATTRIBUTES + self.NUM_FILTERS)*self.NUM_TIMES))
                                 
        else:    
            # 1. Pivot to get filters in each row
            src = src.pivot_table(index=['objectId', 'time_index'], 
                                  columns=['filter'], 
                                  values=self.attributes,
                                  dropna=False)

            # 2. Collapse multi-indexed column using filter_property formatting
            src.columns = src.columns.map('{0[1]}_{0[0]}'.format)
            assert np.array_equal(src.shape, 
                                  (self.NUM_POSITIVES*self.NUM_TIMES, 
                                   self.NUM_ATTRIBUTES*self.NUM_FILTERS))

            src.reset_index(inplace=True) #.set_index('objectId')
            gc.collect()
            assert np.array_equal(src.shape, 
                                  (self.NUM_POSITIVES*self.NUM_TIMES, 
                                   self.NUM_ATTRIBUTES*self.NUM_FILTERS + 2))
            assert np.array_equal(np.setdiff1d(src.columns.values, self.filtered_attributes), 
                                  np.array(['objectId', 'time_index']))
        
            # 3. Pivot to get time sequence in each row
            src = src.pivot_table(index=['objectId'], 
                                columns=['time_index'], 
                                values=self.filtered_attributes,
                                dropna=False)
            gc.collect()

            # 4. Collapse multi-indexed column using time-filter_property formatting
            src.columns = src.columns.map('{0[1]}-{0[0]}'.format)
            #src = src.reindex(sorted(src.columns), axis=1, copy=False)

            self.timed_filtered_attributes = [str(t) + '-' + a for a, t in\
                                              list(product(self.filtered_attributes, range(self.NUM_TIMES)))]
            assert len(self.timed_filtered_attributes) == self.NUM_FILTERS*self.NUM_ATTRIBUTES*self.NUM_TIMES
            #assert np.array_equal(src.columns.values, np.sort(self.timed_filtered_attributes))
            assert np.array_equal(src.shape, 
                                  (self.NUM_POSITIVES, 
                                   self.NUM_ATTRIBUTES*self.NUM_FILTERS*self.NUM_TIMES))
        
        # Set null values to some arbitrary out-of-range value
        # TODO make the value even more meaningless
        src[src.isnull()] = -9999.0
        if self.DEBUG:
            print("Number of null slots: ", src.isnull().sum().sum())
        gc.collect()
        
        if self.onehot_filters:
            X = src.values.reshape(self.NUM_POSITIVES,
                                    self.NUM_ATTRIBUTES + self.NUM_FILTERS,
                                    self.NUM_TIMES).swapaxes(1, 2)
        else:
            X = src.values.reshape(self.NUM_POSITIVES, 
                                   self.NUM_FILTERS*self.NUM_ATTRIBUTES,
                                   self.NUM_TIMES).swapaxes(1, 2)
        y = np.ones((self.NUM_POSITIVES, ))*truth_value
        
        return X, y
    
    def combine_lens_nonlens(self, lens_data, nonlens_data):
        X_lens, y_lens = lens_data
        X_nonlens, y_nonlens = nonlens_data
        
        np.random.seed(self.seed)
        
        assert np.array_equal(X_lens.shape, X_nonlens.shape)
        assert len(y_lens) == len(y_nonlens)
        
        X = np.concatenate([X_lens, X_nonlens], axis=0)
        y = np.concatenate([y_lens, y_nonlens], axis=0)
        
        assert X.shape[0] == 2*self.NUM_POSITIVES
        
        return X, y
    
    def shuffle_data(self, X, y):
        
        p = np.random.permutation(2*self.NUM_POSITIVES)
        X = X[p, :]
        y = y[p, ]
        
        return X, y
    
    def source_to_data(self, features_path, labels_path, return_data=False):
        import time
        
        start = time.time()
        
        self.lens, self.nonlens = self.set_balance(self.lens, 
                                                   self.nonlens, 
                                                   observation_cutoff=self.observation_cutoff)
        self.lens, self.nonlens = self.set_additional_columns(self.lens, self.nonlens)
        X_lens, y_lens = self.make_data_array(self.lens, truth_value=1)
        X_nonlens, y_nonlens = self.make_data_array(self.nonlens, truth_value=0)
        X, y = self.combine_lens_nonlens(lens_data=(X_lens, y_lens),
                                    nonlens_data=(X_nonlens, y_nonlens))
        X, y = self.shuffle_data(X, y)
        gc.collect()
        
        np.save(features_path, X)
        np.save(labels_path, y)
        # Since savetxt only takes 1d or 2d arrays
        #X = X.reshape(2*self.NUM_POSITIVES, -1)
        #np.savetxt(features_path, X, delimiter=",")
        #np.savetxt(label_path, y, delimiter=",")
        
        end = time.time()
        print("Done making the dataset in %0.2f seconds." %(end-start))
        
        if return_data:
            return X, y