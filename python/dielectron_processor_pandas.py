import awkward
import awkward as ak
import numpy as np
# np.set_printoptions(threshold=sys.maxsize)
import pandas as pd

import coffea.processor as processor
from coffea.lookup_tools import extractor
from coffea.lookup_tools import txt_converters, rochester_lookup
from coffea.lumi_tools import LumiMask
# from cachetools import LRUCache

from python.utils import p4_sum, delta_r, rapidity, cs_variables, find_dielectron, bbangle
from python.timer import Timer
from python.weights import Weights
from python.corrections import musf_lookup, musf_evaluator, pu_lookup
#from python.corrections import pu_evaluator
from python.corrections import apply_roccor, fsr_recovery, apply_geofit
from python.mass_resolution import mass_resolution_purdue

from config.parameters import parameters
#from config.variables import variables

from python.corrections_.pu_reweight import pu_lookups, pu_evaluator

class DielectronProcessor(processor.ProcessorABC):
    def __init__(self, **kwargs):
        self.samp_info = kwargs.pop('samp_info', None)
        do_timer = kwargs.pop('do_timer', True)

        if self.samp_info is None:
            print("Samples info missing!")
            return

        self.do_pu = True
        self.auto_pu = True
        self.year = self.samp_info.year
        self.do_roccor = False
        self.do_fsr = False
        self.do_geofit = False

        self.parameters = {
            k: v[self.year] for k, v in parameters.items()}

        self.timer = Timer('global') if do_timer else None

       # self._columns = self.parameters["proc_columns"]

        self.regions = self.samp_info.regions
        self.channels = self.samp_info.channels

        self.lumi_weights = self.samp_info.lumi_weights

        #self.vars_to_save = set([v.name for v in variables])
        self.prepare_lookups()

        # Prepare lookups for corrections
        rochester_data = txt_converters.convert_rochester_file(
            self.parameters["roccor_file"], loaduncs=True
        )
        self.roccor_lookup = rochester_lookup.rochester_lookup(
            rochester_data
        )
        self.musf_lookup = musf_lookup(self.parameters)

        """
        self.pu_lookup = pu_lookup(self.parameters)
        self.pu_lookup_up = pu_lookup(self.parameters, 'up')
        self.pu_lookup_down = pu_lookup(self.parameters, 'down')
        """

        # Prepare evaluator for corrections that can be loaded together
        zpt_filename = self.parameters['zpt_weights_file']

        self.extractor = extractor()
        self.extractor.add_weight_sets([f"* * {zpt_filename}"])

        for mode in ["Data", "MC"]:
            label = f"res_calib_{mode}_{self.year}"
            path = self.parameters['res_calib_path']
            file_path = f"{path}/{label}.root"
            self.extractor.add_weight_sets(
                [f"{label} {label} {file_path}"]
            )

        self.extractor.finalize()
        self.evaluator = self.extractor.make_evaluator()

        if '2016' in self.year:
            self.zpt_path = 'zpt_weights/2016_value'
        else:
            self.zpt_path = 'zpt_weights/2017_value'
        self.evaluator[self.zpt_path]._axes =\
            self.evaluator[self.zpt_path]._axes[0]


    @property
    def accumulator(self):
        return self._accumulator

    @property
    def columns(self):
        return self._columns

    def process(self, df):
        # ------------------------------------------------------------#
        # Filter out events not passing HLT or having
        # less than 2 muons.
        # ------------------------------------------------------------#

        # Initialize timer
        if self.timer:
            self.timer.update()

        # Dataset name (see definitions in config/datasets.py)
        dataset = df.metadata['dataset']
        
        is_mc = 'data' not in dataset
        #print('check 1')
        #print(df.values)
        # ------------------------------------------------------------#
        # Apply HLT, lumimask, genweights, PU weights
        # and L1 prefiring weights
        # ------------------------------------------------------------#

        numevents = len(df)

        if is_mc:
            nTrueInt = df.Pileup.nTrueInt
        else:
            nTrueInt = np.zeros(numevents, dtype=np.float32)

        # All variables that we want to save
        # will be collected into the 'output' dataframe
        output = pd.DataFrame({'run': df.run, 'event': df.event})
        output.index.name = 'entry'
        output['npv'] = df.PV.npvs
        output['nTrueInt'] = nTrueInt
        output['met'] = df.MET.pt

        # Separate dataframe to keep track on weights
        # and their systematic variations
        weights = Weights(output)
        #print('check 2')
        if is_mc:
            # For MC: Apply gen.weights, pileup weights, lumi weights,
            # L1 prefiring weights
            mask = np.ones(numevents, dtype=bool)
            genweight = df.genWeight
            weights.add_weight('genwgt', genweight)
            nTrueInt = np.array(nTrueInt)
            """
            if self.auto_pu:
                self.pu_lookup = pu_lookup(
                    self.parameters, 'nom', auto=nTrueInt
                )
                self.pu_lookup_up = pu_lookup(
                    self.parameters, 'up', auto=nTrueInt
                )
                self.pu_lookup_down = pu_lookup(
                    self.parameters, 'down', auto=nTrueInt
                )
                pu_weight = pu_evaluator(
                    self.pu_lookup, numevents, nTrueInt
                )
                pu_weight_up = pu_evaluator(
                    self.pu_lookup_up, numevents, nTrueInt
                )
                pu_weight_down = pu_evaluator(
                    self.pu_lookup_down, numevents, nTrueInt
                )
                weights.add_weight_with_variations(
                    'pu_wgt', pu_weight, pu_weight_up, pu_weight_down
                )
            """
            if self.do_pu:
                pu_wgts = pu_evaluator(
                    self.pu_lookups,
                    self.parameters,
                    numevents,
                    np.array(df.Pileup.nTrueInt),
                    self.auto_pu
                )
                weights.add_weight_with_variations(
                    'pu_wgt', pu_wgts['nom'], pu_wgts['up'], pu_wgts['down']
                )
            weights.add_weight('lumi', self.lumi_weights[dataset])
            #l1pfw = ak.to_pandas(df.L1PreFiringWeight)
            #if self.parameters["do_l1prefiring_wgts"]:
            #    weights.add_weight_with_variations(
            #        'l1prefiring_wgt', l1pfw.Nom, l1pfw.Up, l1pfw.Dn
            #    )

        else:
            # For Data: apply Lumi mask
            lumi_info = LumiMask(self.parameters['lumimask'])
            mask = lumi_info(df.run, df.luminosityBlock)
        #print('check 3')
        # Apply HLT to both Data and MC
        hlt = ak.to_pandas(df.HLT[self.parameters["el_hlt"]])
        hlt = hlt[self.parameters["el_hlt"]].sum(axis=1)
        #print('check 4 ')
        if self.timer:
            self.timer.add_checkpoint("Applied HLT and lumimask")

        # ------------------------------------------------------------#
        # Update muon kinematics with Rochester correction,
        # FSR recovery and GeoFit correction
        # Raw pT and eta are stored to be used in event selection
        # ------------------------------------------------------------#

        # Save raw variables before computing any corrections
        df['Electron', 'pt_raw'] = df.Electron.pt
        df['Electron', 'eta_raw'] = df.Electron.eta
        df['Electron', 'phi_raw'] = df.Electron.phi
        #df['Muon', 'tkRelIso'] = df.Muon.tkRelIso
        #print ('check 5')
        # Rochester correction
        if self.do_roccor:
            apply_roccor(df, self.roccor_lookup, is_mc)
            df['Muon', 'pt'] = df.Muon.pt_roch

            if self.timer:
                self.timer.add_checkpoint("Rochester correction")

            # variations will be in branches pt_roch_up and pt_roch_down
            # muons_pts = {
            #     'nominal': df.Muon.pt,
            #     'roch_up':df.Muon.pt_roch_up,
            #     'roch_down':df.Muon.pt_roch_down
            # }

        # for ...
        if True:  # indent reserved for loop over muon pT variations
            # According to HIG-19-006, these variations have negligible
            # effect on significance, but it's better to have them
            # implemented in the future

            # FSR recovery
            if self.do_fsr:
                has_fsr = fsr_recovery(df)
                df['Muon', 'pt'] = df.Muon.pt_fsr
                df['Muon', 'eta'] = df.Muon.eta_fsr
                df['Muon', 'phi'] = df.Muon.phi_fsr
                df['Muon', 'pfRelIso04_all'] = df.Muon.iso_fsr

                if self.timer:
                    self.timer.add_checkpoint("FSR recovery")

            # if FSR was applied, 'pt_fsr' will be corrected pt
            # if FSR wasn't applied, just copy 'pt' to 'pt_fsr'
            df['Muon', 'pt_fsr'] = df.Muon.pt

            # GeoFit correction
            if self.do_geofit and ('dxybs' in df.Muon.fields):
                apply_geofit(df, self.year, ~has_fsr)
                df['Muon', 'pt'] = df.Muon.pt_fsr

                if self.timer:
                    self.timer.add_checkpoint("GeoFit correction")

            # --- conversion from awkward to pandas --- #
            # TODO: convert only relevant fields to improve memory usage
            el_branches = ['pt_raw','pt', 'eta', 'eta_raw', 'phi', 'phi_raw', 'mass','cutBased_HEEP','charge']
            electrons = ak.to_pandas(df.Electron[el_branches])
            if self.timer:
                    self.timer.add_checkpoint("load muon data")
            # --------------------------------------------------------#
            # Select muons that pass pT, eta, isolation cuts,
            # muon ID and quality flags
            # Select events with 2 OS muons, no electrons,
            # passing quality cuts and at least one good PV
            # --------------------------------------------------------#
            #print('check 7')
            # Apply event quality flag
            flags = ak.to_pandas(df.Flag)
            flags = flags[self.parameters["event_flags"]].product(axis=1)
            electrons['pass_flags'] = True
            #if self.parameters["muon_flags"]:
            #    muons['pass_flags'] = muons[
            #        self.parameters["muon_flags"]
            #    ].product(axis=1)
            #print('check 8')
            # Define baseline muon selection (applied to pandas DF!)
            electrons['selection'] =  (
                (electrons.pt_raw > self.parameters["electron_pt_cut"]) &
                (abs(electrons.eta_raw) <
                 self.parameters["electron_eta_cut"]) &
                (np.min(electrons.eta_raw) < 1.442) &
                #(muons.tkRelIso <
                # self.parameters["muon_iso_cut"]) &
                (electrons[self.parameters["electron_id"]]>0) 
                #(muons.dxy <
                # self.parameters["muon_dxy"])&
                #((muons.ptErr.values/muons.pt.values)<
                # self.parameters["muon_ptErr/pt"]) &

                #muons.pass_flags
            )
            #print('check 9')
            # Count muons
            nelectrons = electrons[electrons.selection].reset_index()\
                .groupby('entry')['subentry'].nunique()

            # Find opposite-sign muons
            #sum_charge = muons.loc[muons.selection, 'charge']\
            #    .groupby('entry').sum()

            # Veto events with good quality electrons
            #electrons = df.Electron[
            #    (df.Electron.pt > self.parameters["electron_pt_cut"]) &
            #    (abs(df.Electron.eta) <
            #     self.parameters["electron_eta_cut"]) &
            #    (df.Electron[self.parameters["electron_id"]] == 1)
            #]
            #print('check 10')
            #electron_veto = ak.to_numpy(ak.count(electrons.pt, axis=1) == 0)

            # Find events with at least one good primary vertex
            #good_pv = ak.to_pandas(df.PV).npvsGood > 0

            # Define baseline event selection
            #print('check 11')
            #output['two_muons'] = ((nmuons == 2) | (nmuons > 2))
            #output['two_muons'] = output['two_muons'].fillna(False)
            #print("two muons")
            #print(nmuons.values)
           
            output['event_selection'] = (mask &
                (hlt > 0) &
                #(flags > 0) &
                (nelectrons >= 2) 
                #(abs(sum_charge)<nmuons) &
                #good_pv
            )
            #output=output[muons.selection & (nmuons >= 2) & (abs(sum_charge)<nmuons)]
            if self.timer:
                self.timer.add_checkpoint("Selected events and muons")

            # --------------------------------------------------------#
            # Initialize muon variables
            # --------------------------------------------------------#
            #print('check 11')
            # Find pT-leading and subleading muons
            electrons = electrons[electrons.selection & (nelectrons >= 2)]
            #print(muons.columns)
            #if muons.shape[0] !=0:
                #output = output[output['event_selection']]
                #print (output.shape)
                #return output
            #print(muons.shape)
            if self.timer:
                    self.timer.add_checkpoint("muon object selection")
            #print('check 12')
            e1_variable_names = [
                'e1_pt',
                'e1_eta', 'e1_phi'
            ]
            e2_variable_names = [
                'e2_pt', 
                'e2_eta', 'e2_phi'
            ]
            dielectron_variable_names = [
                'dielectron_mass',
                'dielectron_mass_res', 'dielectron_mass_res_rel',
                'dielectron_ebe_mass_res', 'dielectron_ebe_mass_res_rel',
                'dielectron_pt', 'dielectron_pt_log',
                'dielectron_eta', 'dielectron_phi',
                'dielectron_dEta', 'dielectron_dPhi',
                'dielectron_dR', 'dielectron_rap',
                'dielecron_cos_theta_cs', 'dielectron_phi_cs', 'wgt_nominal','pu_wgt'
            ]
            v_names = (
                e1_variable_names +
                e2_variable_names +
                dielectron_variable_names
            )
            output['r'] = None
            output['s'] = dataset
            output['year'] = int(self.year)
            #print(output.shape[1])
            # Initialize columns for muon variables
            #print("check 14")
            for n in (v_names):
                output[n] = 0.0

            #if muons.shape[0] == 0:
            #    output = output.reindex(sorted(output.columns), axis=1)
                #print('p 6')
            #    output = output[output.r.isin(self.regions)]

            #    return output


            #print(muons.shape)
            result = electrons.groupby('entry').apply(find_dielectron)
            dielectron=pd.DataFrame(result.to_list(),columns=['idx1','idx2','mass'])
            e1=electrons.loc[dielectron.idx1.values,:]
            e2=electrons.loc[dielectron.idx2.values,:]
            e1.index = e1.index.droplevel('subentry')
            e2.index = e2.index.droplevel('subentry')
            if self.timer:
                self.timer.add_checkpoint("dimuon pair selection")
            #print('flag6')
            #import sys
            #sys.exit() 
            #print("check 13")
            #output['bbangle'] = bbangle(mu1, mu2)
            #print("finish")
            #output['event_selection'] = (
            #    output.event_selection & (output.bbangle>self.parameters['3dangle'])
            #)
  
            if self.timer:
                self.timer.add_checkpoint("back back angle calculation")

            dielectron_mass=dielectron.mass

            # --------------------------------------------------------#
            # Select events with muons passing leading pT cut
            # and trigger matching
            # --------------------------------------------------------#

            # Events where there is at least one muon passing
            # leading muon pT cut
            
            #pass_leading_pt = (
            #    mu1.pt_raw > self.parameters["muon_leading_pt"]
            #)
            #print(pass_leading_pt)
            # update event selection with leading muon pT cut
            #output['pass_leading_pt'] = pass_leading_pt
            #output['event_selection'] = (
            #    output.event_selection  & (bbangle(mu1, mu2) < self.parameters['3dangle'] )
            #)

            #if self.timer:
            #    self.timer.add_checkpoint("Applied trigger matching")

            # --------------------------------------------------------#
            # Fill dimuon and muon variables
            # --------------------------------------------------------#
            # Fill single muon variables
            #print("check 15")

            for v in ['pt', 'eta', 'phi']:
                output[f'e1_{v}'] = e1[v]
                output[f'e2_{v}'] = e2[v]
            #output['mu1_iso'] = mu1.tkRelIso
            #output['mu2_iso'] = mu2.tkRelIso
            output.dielectron_mass=dielectron_mass
            #output['mu1_pt_over_mass'] = output.mu1_pt.values / output.dimuon_mass.values
            #output['mu2_pt_over_mass'] = output.mu2_pt.values / output.dimuon_mass.values
            
        
            if self.timer:
                    self.timer.add_checkpoint("all muon variables")
            # Fill dimuon variables
            #print("check 16")
            mm = p4_sum(e1, e2)
            for v in ['pt', 'eta', 'phi', 'mass', 'rap']:
                name = f'dielectron_{v}'
                output[name] = mm[v]
                output[name] = output[name].fillna(-999.)
            #print("dimuon pt")
            #print(output.dimuon_pt.values)
            output['dielectron_pt_log'] = np.log(output.dielectron_pt[output.dielectron_pt>0])
            output.loc[output.dielectron_pt<0, 'dielectron_pt_log']=-999.
            #print("finish")
            mm_deta, mm_dphi, mm_dr = delta_r(
                e1.eta, e2.eta,
                e1.phi, e2.phi
            )
            output['dielectron_pt'] = mm.pt
            output['dielectron_eta'] = mm.eta
            output['dielectron_phi'] = mm.phi
            output['dielectron_dEta'] = mm_deta
            output['dielectron_dPhi'] = mm_dphi
            output['dielectron_dR'] = mm_dr
            #print("check 17")
            #output['dimuon_ebe_mass_res'] = mass_resolution_purdue(
            #                                    is_mc,
            #                                    self.evaluator,
            #                                    output,
            #                                    self.year
            #                                )
            #output['dimuon_ebe_mass_res_rel'] = (
            #    output.dimuon_ebe_mass_res / output.dimuon_mass
            #)
            if self.timer:
                    self.timer.add_checkpoint("add dimuon variable")
            #print("check 18")
            output['dielectron_cos_theta_cs'],\
                output['dielectron_phi_cs'] = cs_variables(e1, e2)
            if self.timer:
                self.timer.add_checkpoint("Calculate CS angle")

        # ------------------------------------------------------------#
        # Calculate other event weights
        # ------------------------------------------------------------#

        if is_mc:
            # do_zpt = ('dy' in dataset)
            do_musf = False

            """
            if do_zpt:
                zpt_weight = np.ones(numevents, dtype=float)
                zpt_weight[two_muons] =\
                    self.evaluator[self.zpt_path](
                        output['dimuon_pt'][two_muons]
                    ).flatten()
                weights.add_weight('zpt_wgt', zpt_weight)
            """

            if do_musf:
                #print("check 19")
                sf = musf_evaluator(
                    self.musf_lookup,
                    self.year,
                    numevents,
                    mu1, mu2
                )

                weights.add_weight_with_variations(
                    'muID', sf['muID_nom'],
                    sf['muID_up'], sf['muID_down']
                )
                weights.add_weight_with_variations(
                    'muIso', sf['muIso_nom'],
                    sf['muIso_up'], sf['muIso_down']
                )
                weights.add_weight_with_variations(
                    'muTrig', sf['muTrig_nom'],
                    sf['muTrig_up'], sf['muTrig_down']
                )
            else:
                #print("check 20")
                weights.add_dummy_weight_with_variations('muID')
                weights.add_dummy_weight_with_variations('muIso')
                weights.add_dummy_weight_with_variations('muTrig')


        if self.timer:
            self.timer.add_checkpoint("Computed event weights")

        # ------------------------------------------------------------#
        # Fill outputs
        # ------------------------------------------------------------#
        #print ("p 4")
        mass = output.dielectron_mass
        #print("check 21")
        #output['r'] = None
        output.loc[((output.e1_eta < 1.442) & (output.e2_eta < 1.442)), 'r'] = "bb"
        output.loc[((output.e1_eta > 1.442) | (output.e2_eta > 1.442)), 'r'] = "be"
        #output['s'] = dataset
        #output['year'] = int(self.year)
        #print("check 22")
        for wgt in weights.df.columns:
            #print(wgt)
            if (wgt!='nominal'):
                continue
            output[f'wgt_{wgt}'] = weights.get_weight(wgt)

        #NNPDFFac = 0.919027 + (5.98337e-05)*mass + (2.56077e-08)*mass**2 + (-2.82876e-11)*mass**3 + (9.2782e-15)*mass**4 + (-7.77529e-19)*mass**5
        #print(NNPDFFac.head())
        #NNPDFFac_bb = 0.911563 + (0.000113313)*mass + (-2.35833e-08)*mass**2 + (-1.44584e-11)*mass*3 + (8.41748e-15)*mass**4 + (-8.16574e-19)*mass**5
        #NNPDFFac_be = 0.934502 + (2.21259e-05)*mass + (4.14656e-08)*mass**2 + (-2.26011e-11)*mass**3 + (5.58804e-15)*mass**4 + (-3.92687e-19)*mass**5
        #output.loc[((output.mu1_eta < 1.2) & (output.mu2_eta < 1.2)) ,'wgt_nominal'] = output.loc[((output.mu1_eta < 1.2) & (output.mu2_eta < 1.2)) ,'wgt_nominal'] * NNPDFFac_bb
        #output.loc[((output.mu1_eta > 1.2) | (output.mu2_eta > 1.2)) ,'wgt_nominal'] = output.loc[((output.mu1_eta > 1.2) | (output.mu2_eta > 1.2)) ,'wgt_nominal'] * NNPDFFac_be
        #print('p 5')
        #print(output['wgt_nominal'].values)        
        output = output.loc[output.event_selection, :]
        output = output.reindex(sorted(output.columns), axis=1)
        #print('p 6')
        output = output[output.r.isin(self.regions)]
        #print('p 7')  
        if self.timer:
            self.timer.add_checkpoint("Filled outputs")
            self.timer.summary()

        return output


    def prepare_lookups(self):
        """
        # Rochester correction
        rochester_data = txt_converters.convert_rochester_file(
            self.parameters["roccor_file"], loaduncs=True
        )
        self.roccor_lookup = rochester_lookup.rochester_lookup(
            rochester_data
        )

        # Muon scale factors
        self.musf_lookup = musf_lookup(self.parameters)
        """
        # Pile-up reweighting
        self.pu_lookups = pu_lookups(self.parameters)
        
        """
        # --- Evaluator
        self.extractor = extractor()

        # Z-pT reweigting (disabled)
        zpt_filename = self.parameters['zpt_weights_file']
        self.extractor.add_weight_sets([f"* * {zpt_filename}"])
        if '2016' in self.year:
            self.zpt_path = 'zpt_weights/2016_value'
        else:
            self.zpt_path = 'zpt_weights/2017_value'

        # Calibration of event-by-event mass resolution
        for mode in ["Data", "MC"]:
            label = f"res_calib_{mode}_{self.year}"
            path = self.parameters['res_calib_path']
            file_path = f"{path}/{label}.root"
            self.extractor.add_weight_sets(
                [f"{label} {label} {file_path}"]
            )

        self.extractor.finalize()
        self.evaluator = self.extractor.make_evaluator()

        self.evaluator[self.zpt_path]._axes =\
            self.evaluator[self.zpt_path]._axes[0]
        """
        return


    def postprocess(self, accumulator):
        return accumulator
