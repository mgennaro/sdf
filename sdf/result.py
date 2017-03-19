from functools import lru_cache
import os.path
import pickle
import glob
import time

import numpy as np
from scipy.stats import truncnorm
import pymultinest as pmn
import matplotlib.pyplot as plt
import corner
import astropy.units as u

from . import photometry
from . import spectrum
from . import model
from . import filter
from . import fitting
from . import utils
from . import config as cfg


class Result(object):
    """Class to compute and handle multinest results."""

    @lru_cache(maxsize=128)
    def __init__(self,rawphot,model_comps):
        """Basic instantiation of the Result object."""
        
        self.file_info(rawphot,model_comps)
    
    
    @lru_cache(maxsize=128)
    def file_info(self,rawphot,model_comps):
        """Basic file info."""
                  
        # component info
        self.model_comps = model_comps
        self.star_or_disk = ()
        for comp in model_comps:
            if comp in cfg.models['star']:
                self.star_or_disk += ('star',)
            elif comp in cfg.models['disk']:
                self.star_or_disk += ('disk',)
            else:
                raise utils.SdfError("couldn't assigm comp {} to star or disk "
                               "given lists in {} and {}".
                               format(comp,cfg.models['star'],
                                      cfg.models['disk']))
    
        self.n_comps = len(model_comps)
        
        # where the rawphot file is
        self.rawphot = rawphot
        self.path = os.path.dirname(rawphot)
        
        # id
        self.id = os.path.basename(rawphot).rstrip('-rawphot.txt')

        # where the multinest output is (or will be), create if needed
        self.pmn_dir = self.path + '/' + self.id            \
                       + cfg.fitting['pmn_dir_suffix']
        if not os.path.exists(self.pmn_dir):
            os.mkdir(self.pmn_dir)
        
        # the base name for multinest files
        self.pmn_base = self.pmn_dir + '/'                  \
                        + '+'.join(self.model_comps)        \
                        + cfg.fitting['pmn_model_suffix']

        # pickle file, may not exist yet
        self.pickle = self.pmn_base + '.pkl'


    @lru_cache(maxsize=128)
    def get(rawphot,model_comps,update_mn=False,update_an=False,
            nospec=False):
        """Take photometry file and model_name, and fill the rest."""

        self = Result(rawphot,model_comps)

        # see if we have a pickle of results to return, checking that
        # it's more recent than the multinest output and the phot file
        if not update_mn and not update_an and os.path.exists(self.pickle):
            if os.path.getmtime(self.pickle) >       \
                os.path.getmtime(self.rawphot) and   \
                os.path.getmtime(self.pickle) >      \
                os.path.getmtime(self.pmn_base+'phys_live.points'):
                with open(self.pickle,'rb') as f:
                    self = pickle.load(f)
                
                # update object with local file info before returning
                self.file_info(rawphot,model_comps)
                return self

        # observations; keywords, tuples of photometry and spectra. if
        # there is nothing in the photometry file then don't fill
        # anything else
        p = photometry.Photometry.read_sdb_file(self.rawphot)
        if p is None:
            return

        self.obs = (p,)
        self.obs_keywords = utils.get_sdb_keywords(self.rawphot)
        if not nospec:
            s = spectrum.ObsSpectrum.read_sdb_file(self.rawphot,
                                                   module_split=True,
                                                   nspec=1)
            if s is not None:
                self.obs = (p,) + s

        # models
        mod,plmod = model.get_models(self.obs,self.model_comps)
        self.models = mod
        self.pl_models = plmod
        self.model_info = model.models_info(self.models)

        # if we want to re-run multinest, delete previous output first
        run_mn = update_mn
        if os.path.exists(self.pmn_base+'phys_live.points'):
            if os.path.getmtime(self.rawphot) > \
                os.path.getmtime(self.pmn_base+'phys_live.points'):
                run_mn = True

        if run_mn:
            self.delete_multinest()
        
        # go there, multinest only takes 100 char paths
        with utils.pushd(self.pmn_dir):
            fitting.multinest( self.obs,self.models,'.' )

        a = pmn.Analyzer(outputfiles_basename=self.pmn_base,
                         n_params=self.model_info['ndim'])
        self.analyzer = a

        # when the multinest results were finished
        self.mtime = os.path.getmtime(self.pmn_base + 'phys_live.points')

        # parameter corner plot if needed
        self.corner_plot = self.pmn_base+'corner.png'
        plot = update_an
        if not os.path.exists(self.corner_plot):
            plot = True
        else:
            if os.path.getmtime(self.corner_plot) < self.mtime:
                plot = True
            
        if plot:
            d = self.analyzer.get_data()
            fig = corner.corner(d[:,2:],labels=self.model_info['parameters'],
                                show_titles=True)
            fig.savefig(self.corner_plot)
            plt.close(fig) # not doing this causes an epic memory leak

        # parameter names and best fit
        self.evidence = self.analyzer.get_stats()['global evidence']
        self.parameters = self.model_info['parameters']

        self.best_params = []
        self.best_params_1sig = []
        for i in range(len(self.parameters)):
            self.best_params.append(self.analyzer.get_stats()\
                                    ['marginals'][i]['median'])
            self.best_params_1sig.append(self.analyzer.get_stats()\
                                         ['marginals'][i]['sigma'])
        
        # tuple of multinest samples to use for uncertainty estimation
        self.param_samples = ()
        self.param_sample_probs = []
        randi = []
        for i in np.random.randint(0,high=len(self.analyzer.data),
                                   size=cfg.fitting['n_samples']):
            randi.append(i)
            self.param_samples += (self.analyzer.data[i,2:],)
            self.param_sample_probs.append(self.analyzer.data[i,0])

        # as above, split into components
        self.n_parameters = len(self.parameters)
        self.comp_best_params = ()
        self.comp_best_params_1sig = ()
        self.comp_parameters = ()
        self.comp_param_samples = ()
        i0 = 0
        for comp in self.models:
            nparam = len(comp[0].parameters)+1
            self.comp_parameters += (comp[0].parameters,)
            self.comp_best_params += (self.best_params[i0:i0+nparam],)
            self.comp_best_params_1sig += (self.best_params_1sig[i0:i0+nparam],)
            
            comp_i_samples = ()
            for i in randi:
                comp_i_samples += (self.analyzer.data[i,i0+2:i0+nparam+2],)
            self.comp_param_samples += (comp_i_samples,)

            i0 += nparam
        
        # fluxes and uncertainties etc. using parameter samples
        self.distributions = {}
        
        # we will want lstar at 1pc below
        self.distributions['lstar_1pc_tot'] = np.zeros(cfg.fitting['n_samples'])
        
        # generate a normal distribution of parallaxes, truncated to
        # contain no negative values, if there is an uncertainty
        if self.obs_keywords['plx_err'] is not None \
            and self.obs_keywords['plx_value'] is not None:
            if self.obs_keywords['plx_value'] > 0:
                
                lo_cut = -1. * ( self.obs_keywords['plx_value'] /   \
                                 self.obs_keywords['plx_err'] )
                                 
                self.distributions['parallax'] = \
                    truncnorm.rvs(lo_cut,np.inf,
                                  loc=self.obs_keywords['plx_value']/1e3,
                                  scale=self.obs_keywords['plx_err']/1e3,
                                  size=cfg.fitting['n_samples'])
                    
        # observed fluxes, this is largely copied from fitting.residual
        tmp = fitting.concat_obs(self.obs)
        self.obs_fnujy,self.obs_e_fnujy,self.obs_upperlim,self.filters_ignore,\
            obs_ispec,obs_nel,self.wavelengths,self.filters,self.obs_bibcode = tmp
        spec_norm = np.take(self.best_params+[1.0],obs_ispec)
        self.obs_fnujy = self.obs_fnujy * spec_norm
        self.obs_e_fnujy = self.obs_e_fnujy * spec_norm

        # model photometry and residuals, including colours/indices
        model_dist = np.zeros((len(self.filters),cfg.fitting['n_samples']))
        model_comp_dist = np.zeros((self.n_comps,len(self.filters),
                                    cfg.fitting['n_samples']))
                                    
        for i,par in enumerate(self.param_samples):
            model_fnujy,model_comp_fnujy = \
                model.model_fluxes(self.models,par,obs_nel)
        
            model_dist[:,i] = model_fnujy
            model_comp_dist[:,:,i] = model_comp_fnujy

        # summed model fluxes
        self.distributions['model_fnujy'] = model_dist
        lo,self.model_fnujy,hi = fitting.pmn_pc(self.param_sample_probs,
                                                model_dist,[16.0,50.0,84.0],
                                                axis=1)
        self.model_fnujy_1sig_lo = self.model_fnujy - lo
        self.model_fnujy_1sig_hi = hi - self.model_fnujy

        # per-component model fluxes
        self.distributions['model_comp_fnujy'] = model_comp_dist
        lo,self.model_comp_fnujy,hi = fitting.pmn_pc(self.param_sample_probs,
                                                     model_comp_dist,[16.0,50.0,84.0],
                                                     axis=2)
        self.model_comp_fnujy_1sig_lo = self.model_comp_fnujy - lo
        self.model_comp_fnujy_1sig_hi = hi - self.model_comp_fnujy

        # fitting results
        self.residuals,_,_ = fitting.residual(self.best_params,
                                              self.obs,self.models)
        self.chisq = np.sum( np.square( self.residuals ) )
        self.dof = len(self.wavelengths)-len(self.parameters)-1

        # star/disk photometry for all filters
        star_comps = ()
        star_params = []
        star_param_samples = [[] for i in range(cfg.fitting['n_samples'])]
        disk_comps = ()
        disk_params = []
        disk_param_samples = [[] for i in range(cfg.fitting['n_samples'])]

        # first create star/disk component arrays
        for i,comp in enumerate(self.model_comps):
            if self.star_or_disk[i] == 'star':
                star_comps += (comp,)
                star_params += self.comp_best_params[i]
                for j in range(cfg.fitting['n_samples']):
                    star_param_samples[j] = np.append(star_param_samples[j],
                                                      self.comp_param_samples[i][j])
            elif self.star_or_disk[i] == 'disk':
                disk_comps += (comp,)
                disk_params += self.comp_best_params[i]
                for j in range(cfg.fitting['n_samples']):
                    disk_param_samples[j] = np.append(disk_param_samples[j],
                                                      self.comp_param_samples[i][j])

        # compute all star photometry for each parameter sample
        p_all = photometry.Photometry(filters=filter.Filter.all)
        if len(star_comps) > 0:
            star_mod,_ = model.get_models((p_all,),star_comps)
            
            star_phot_dist = np.zeros((p_all.nphot,cfg.fitting['n_samples']))
            for i,par in enumerate(star_param_samples):
                tmp,_ = model.model_fluxes(star_mod,par,[p_all.nphot])
                star_phot_dist[:,i] = tmp

            self.distributions['star_phot'] = star_phot_dist
            lo,self.star_phot,hi = fitting.pmn_pc(self.param_sample_probs,
                                                  star_phot_dist,[16.0,50.0,84.0],
                                                  axis=1)
            self.star_phot_1sig_lo = self.star_phot - lo
            self.star_phot_1sig_hi = hi - self.star_phot

        else:
            self.star_phot = None

        # repeat for disk photometry
        if len(disk_comps) > 0:
            disk_mod,_ = model.get_models((p_all,),disk_comps)

            disk_phot_dist = np.zeros((p_all.nphot,cfg.fitting['n_samples']))
            for i,par in enumerate(disk_param_samples):
                tmp,_ = model.model_fluxes(disk_mod,par,[p_all.nphot])
                disk_phot_dist[:,i] = tmp

            self.distributions['disk_phot'] = disk_phot_dist
            lo,self.disk_phot,hi = fitting.pmn_pc(self.param_sample_probs,
                                                  disk_phot_dist,[16.0,50.0,84.0],
                                                  axis=1)
            self.disk_phot_1sig_lo = self.disk_phot - lo
            self.disk_phot_1sig_hi = hi - self.disk_phot

        else:
            self.disk_phot = None
        
        self.all_filters = p_all.filters
        
        # total photometry
        mod,_ = model.get_models((p_all,),self.model_comps)
        all_phot_dist = np.zeros((p_all.nphot,cfg.fitting['n_samples']))
        for i,par in enumerate(self.param_samples):
            tmp,_ = model.model_fluxes(mod,par,[p_all.nphot])
            all_phot_dist[:,i] = tmp
        
        self.all_phot_dist = all_phot_dist

        self.distributions['all_phot'] = all_phot_dist
        lo,self.all_phot,hi = fitting.pmn_pc(self.param_sample_probs,
                                             all_phot_dist,[16.0,50.0,84.0],
                                             axis=1)
        self.all_phot_1sig_lo = self.all_phot - lo
        self.all_phot_1sig_hi = hi - self.all_phot

        # ObsSpectrum for each component, all the same wavelengths
        wave = cfg.models['default_wave']
        star_spec = np.zeros(len(wave))
        disk_spec = np.zeros(len(wave))
        self.comp_spectra = ()
        for i,comp in enumerate(self.pl_models):
            for mtmp in comp:
                if not isinstance(mtmp,model.SpecModel):
                    continue
                
                m = mtmp.copy()
                m.interp_to_wavelengths(wave)

                s = spectrum.ObsSpectrum(wavelength=m.wavelength,
                                         fnujy=m.fnujy(self.comp_best_params[i]))
                s.fill_irradiance()
                self.comp_spectra += (s,)
    
                if self.star_or_disk[i] == 'star':
                    star_spec += m.fnujy(self.comp_best_params[i])
                elif self.star_or_disk[i] == 'disk':
                    disk_spec += m.fnujy(self.comp_best_params[i])

        # and star/disk spectra
        if np.max(star_spec) > 0:
            self.star_spec = spectrum.ObsSpectrum(wavelength=wave,fnujy=star_spec)
        else:
            self.star_spec = None

        if np.max(disk_spec) > 0:
            self.disk_spec = spectrum.ObsSpectrum(wavelength=wave,fnujy=disk_spec)
        else:
            self.disk_spec = None

        # model-specifics, also combined into a single tuple
        self.star,self.star_distributions = self.star_results()
        self.disk_r,self.disk_r_distributions = self.disk_r_results()
        self.main_results = self.star + self.disk_r
        
        # corner plot of distributions
        samples = np.zeros(cfg.fitting['n_samples'])
        labels = []
        for dist in self.star_distributions:
            for key in dist.keys():
                samples = np.vstack((samples,dist[key]))
                labels.append(key)
        for dist in self.disk_r_distributions:
            for key in dist.keys():
                samples = np.vstack((samples,dist[key]))
                labels.append(key)
        samples = samples[1:]

        # corner plot if needed
        self.distributions_plot = self.pmn_base+'distributions.png'
        plot = update_an
        if not os.path.exists(self.distributions_plot):
            plot = True
        else:
            if os.path.getmtime(self.distributions_plot) < self.mtime:
                plot = True
            
        if plot:
            fig = corner.corner(samples.transpose(),labels=labels,
                                show_titles=True)
            fig.savefig(self.distributions_plot)
            plt.close(fig)

        # delete the models to save space, we don't need them again
        self.models = ''
        self.pl_models = ''

        # save for later in a pickle, updating the mtime to now
        self.mtime = time.time()
        with open(self.pickle,'wb') as f:
            pickle.dump(self,f)

        return self
            

    def star_results(self):
        """Return tuple of dicts of star-specifics, if result has star."""

        star = ()
        distributions = ()
        for i,comp in enumerate(self.model_comps):
            if comp in cfg.models['star']:
                star_one, dist_one = self.star_results_one(i)
                star = star + (star_one,)
                distributions = distributions + (dist_one,)

        return star,distributions


    def star_results_one(self,i):
        """Return dict of star-specifics for ith model component."""

        star = {}
        distributions = {}
        for j,par in enumerate(self.comp_parameters[i]):
            star[par] = self.best_params[j]
            star['e_'+par] = self.best_params_1sig[j]
        
        # stellar luminosity at 1pc, uncertainty is normalisation
        lstar_1pc_dist = np.zeros(cfg.fitting['n_samples'])
        for j,par in enumerate(self.comp_param_samples[i]):
            
            # there will only be one SpecModel in the ith component
            for m in self.pl_models[i]:
                if not isinstance(m,model.SpecModel):
                    continue
                s = spectrum.ObsSpectrum(wavelength=m.wavelength,
                                         fnujy=m.fnujy(par))
                s.fill_irradiance()

            lstar_1pc_dist[j] = s.irradiance \
                        * 4 * np.pi * (u.pc.to(u.m))**2 / u.L_sun.to(u.W)
        
        distributions['lstar_1pc'] = lstar_1pc_dist
        self.distributions['lstar_1pc_tot'] += lstar_1pc_dist
        lo,star['lstar_1pc'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                 lstar_1pc_dist,[16.0,50.0,84.0])
        star['e_lstar_1pc_lo'] = star['lstar_1pc'] - lo
        star['e_lstar_1pc_hi'] = hi - star['lstar_1pc']
        star['e_lstar_1pc'] = (star['e_lstar_1pc_lo']+star['e_lstar_1pc_hi'])/2.0
    
        # distance-dependent params
        if 'parallax' in self.distributions.keys():

            star['plx_arcsec'] = self.obs_keywords['plx_value'] / 1e3
            star['e_plx_arcsec'] = self.obs_keywords['plx_err'] / 1e3

            # combine lstar_1pc and plx distributions for lstar
            lstar_dist = lstar_1pc_dist / self.distributions['parallax']**2
            distributions['lstar'] = lstar_dist
            lo,star['lstar'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                 lstar_dist,[16.0,50.0,84.0])
            star['e_lstar_lo'] = star['lstar'] - lo
            star['e_lstar_hi'] = hi - star['lstar']
            star['e_lstar'] = (star['e_lstar_lo']+star['e_lstar_hi'])/2.0
            
            rstar_dist = np.zeros(cfg.fitting['n_samples'])
            for j,par in enumerate(self.comp_param_samples[i]):
                rstar_dist[j] = np.sqrt(cfg.ssr * 10**par[-1]/np.pi) \
                    * u.pc.to(u.m) / self.distributions['parallax'][j] / u.R_sun.to(u.m)
            
            distributions['rstar'] = rstar_dist
            lo,star['rstar'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                 rstar_dist,[16.0,50.0,84.0])
            star['e_rstar_lo'] = star['rstar'] - lo
            star['e_rstar_hi'] = hi - star['rstar']
            star['e_rstar'] = (star['e_rstar_lo']+star['e_rstar_hi'])/2.0
                
        return (star,distributions)


    def disk_r_results(self):
        """Return tuple of dicts of disk-specifics, if result has disk_r."""

        disk_r = ()
        distributions = ()
        for i,comp in enumerate(self.model_comps):
            if comp in cfg.models['disk_r']:
                disk_r_one,dist_one = self.disk_r_results_one(i)
                disk_r = disk_r + (disk_r_one,)
                distributions = distributions + (dist_one,)

        return disk_r,distributions
    
    
    def disk_r_results_one(self,i):
        """Return dict of disk_r-specifics for ith model component."""

        disk_r = {}
        distributions = {}
        for j,par in enumerate(self.comp_parameters[i]):
            if 'log_' in par:
                par_in = par.replace('log_','')
                disk_r[par_in] = 10**self.comp_best_params[i][j]
                disk_r['e_'+par_in] = 10**self.comp_best_params_1sig[i][j]
            else:
                par_in = par
                disk_r[par_in] = self.comp_best_params[i][j]
                disk_r['e_'+par_in] = self.comp_best_params_1sig[i][j]
    
            # array of disk temperature samples
            if 'Temp' in par:
                temp_dist = np.zeros(cfg.fitting['n_samples'])
                for k,sample in enumerate(self.comp_param_samples[i]):
                    if par == 'log_Temp':
                        temp_dist[k] = 10**sample[j]
                    elif par == 'Temp':
                        temp_dist[k] = sample[j]

        # disk and fractional luminosity
        ldisk_1pc_dist = np.zeros(cfg.fitting['n_samples'])
        for j,par in enumerate(self.comp_param_samples[i]):
            
            # there will only be one SpecModel in the ith component
            for m in self.pl_models[i]:
                if not isinstance(m,model.SpecModel):
                    continue
                s = spectrum.ObsSpectrum(wavelength=m.wavelength,
                                         fnujy=m.fnujy(par))
                s.fill_irradiance()

            ldisk_1pc_dist[j] = s.irradiance \
                        * 4 * np.pi * (u.pc.to(u.m))**2 / u.L_sun.to(u.W)
        
        distributions['ldisk_1pc'] = ldisk_1pc_dist
        lo,disk_r['ldisk_1pc'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                   ldisk_1pc_dist,[16.0,50.0,84.0])
        disk_r['e_ldisk_1pc_lo'] = disk_r['ldisk_1pc'] - lo
        disk_r['e_ldisk_1pc_hi'] = hi - disk_r['ldisk_1pc']
        disk_r['e_ldisk_1pc'] = (disk_r['e_ldisk_1pc_lo']+disk_r['e_ldisk_1pc_hi'])/2.0
        
        # stellar luminosities (if >1 star) were summed already
        if np.sum(self.distributions['lstar_1pc_tot']) > 0.0:

            ldisk_lstar_dist = ldisk_1pc_dist / self.distributions['lstar_1pc_tot']
            distributions['ldisk_lstar'] = ldisk_lstar_dist
            lo,disk_r['ldisk_lstar'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                         ldisk_lstar_dist,[16.0,50.0,84.0])
            disk_r['e_ldisk_lstar_lo'] = disk_r['ldisk_lstar'] - lo
            disk_r['e_ldisk_lstar_hi'] = hi - disk_r['ldisk_lstar']
            disk_r['e_ldisk_lstar'] = (disk_r['e_ldisk_lstar_lo']+disk_r['e_ldisk_lstar_hi'])/2.0

            # distance (and stellar L)-dependent params
            if 'parallax' in self.distributions.keys():
                lstar = self.distributions['lstar_1pc_tot'] / \
                        self.distributions['parallax']
                rdisk_bb_dist = lstar**0.5 * (278.3/temp_dist)**2

                distributions['rdisk_bb'] = rdisk_bb_dist
                lo,disk_r['rdisk_bb'],hi = fitting.pmn_pc(self.param_sample_probs,
                                                          rdisk_bb_dist,[16.0,50.0,84.0])
                disk_r['e_rdisk_bb_lo'] = disk_r['rdisk_bb'] - lo
                disk_r['e_rdisk_bb_hi'] = hi - disk_r['rdisk_bb']
                disk_r['e_rdisk_bb'] = (disk_r['e_rdisk_bb_lo']+disk_r['e_rdisk_bb_hi'])/2.0

        return (disk_r,distributions)


    def main_results_text(self):
        """Return nicely formatted tuple of text of results."""
    
        # the array sets the order, and the dict the conversion
        text_ord = ['Teff','lstar','rstar',
                    'Temp','rdisk_bb','ldisk_lstar',
                    'lam0','beta','Dmin','q']
                    
        text_sub = {'Teff': ['T<sub>star</sub>','K'],
                    'MH':   ['[M/H]',''],
                    'logg': ['logg',''],
                    'lstar':['L<sub>star</sub>','L<sub>Sun</sub>'],
                    'rstar':['R<sub>star</sub>','R<sub>Sun</sub>'],
                    'Temp': ['T<sub>dust</sub>','K'],
                    'lam0': ['&lambda;<sub>0</sub>','&mu;m'],
                    'beta': ['&beta;',''],
                    'Dmin': ['D<sub>min</sub>','&mu;m'],
                    'q':    ['q',''],
                    'ldisk_lstar':['L<sub>disk</sub>/L<sub>star</sub>',''],
                    'rdisk_bb':['R<sub>BB</sub>','au']}
    
        text = ()
        for res in self.main_results:

            string = ''
            i = 0
            for par in text_ord:
                if par in res.keys():
                    unc,meas = utils.rnd1sf([res['e_'+par],res[par]])
                    if i > 0:
                        string += ' , '
                    string += '{} = {:g} &plusmn; {:g} {}'.format(text_sub[par][0],meas,unc,text_sub[par][1])
                    i += 1

            text = text + (string,)
                
        return text
    
    
    def delete_multinest(self):
        """Delete multinest output so it can be run again."""

        fs = glob.glob(self.pmn_base+'*')
        for f in fs:
            os.remove(f)


def sort_results(results):
    """Return indices to sort a list of Result objects by evidence."""

    ev = []
    ndim = []
    for r in results:

        ndim.append( r.model_info['ndim'] )
        ev.append( r.evidence )

    return fitting.sort_evidence(ev,ndim)
