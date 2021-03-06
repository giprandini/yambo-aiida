import sys
from aiida.backends.utils import load_dbenv, is_dbenv_loaded

if not is_dbenv_loaded():
    load_dbenv()

from aiida.orm import load_node
from aiida.orm.data.upf import get_pseudos_from_structure
from aiida.common.exceptions import InputValidationError,ValidationError
from collections import defaultdict
from aiida.orm.utils import DataFactory, CalculationFactory
from aiida.orm.data.base import Float, Str, NumericType, BaseType, List, Bool
from aiida.orm.code import Code
from aiida.orm.data.structure import StructureData
from aiida.work.run import run, submit
from aiida.work.workchain import WorkChain, while_, ToContext, Outputs
from aiida_yambo.calculations.gw  import YamboCalculation
from aiida.common.links import LinkType
from aiida_yambo.workflows.yambo_utils import default_step_size, update_parameter_field, set_default_qp_param,\
               default_pw_settings, set_default_pw_param, yambo_default_settings, default_qpkrange, p2y_default_settings
from aiida_yambo.workflows.yamborestart  import YamboRestartWf
from aiida_yambo.workflows.yambowf  import YamboWorkflow
from aiida.orm.data.remote import RemoteData
from aiida_quantumespresso.calculations.pw import PwCalculation
from aiida_yambo.calculations.gw  import YamboCalculation
import numpy as np 
from scipy.optimize import  curve_fit 

#PwCalculation = CalculationFactory('quantumespresso.pw')
#YamboCalculation = CalculationFactory('yambo.yambo')

ParameterData = DataFactory("parameter")
KpointsData = DataFactory("array.kpoints")
PwProcess = PwCalculation.process()
YamboProcess = YamboCalculation.process()

"""
GW (Bands)
===========
First target parameters for convergences.
  - BndsRnXp   nbndX 
  - GbndRnge   nbndG
  - PPAPntXp [OK]
  - NGsBlkXp [OK] ecutX
  - FFTGvecs 
  - kpoints

Target quantities for convergence:
  - GW band width along a single kpoint, between two band indexes

Test for convergence:
  - Change between runs smaller than threshold

possible techniques
  1. Coordinate descent
  2. k

#BSE (Optics)   *** THIS WILL WAIT FOR ITS OWN WORKFLOW******
#First target parameters for convergences.
#BSEBands
"""

class YamboConvergenceWorkflow(WorkChain):
    """
    """

    @classmethod
    def define(cls, spec):
        """
        as input we need
         1. variable
         2. threshold
         3. starting points
          converge_parameters = ['BndsRnXp','NGsBlkXp','GbndRnge']
          starting_points = [10,10,10]
        """
        super(YamboConvergenceWorkflow, cls).define(spec)
        spec.input("precode", valid_type=BaseType)
        spec.input("pwcode", valid_type=BaseType, required=False)
        spec.input("yambocode", valid_type=BaseType)
        spec.input("pseudo", valid_type=BaseType,required=True)
        spec.input("calculation_set_pw", valid_type=ParameterData, required=False)
        spec.input("calculation_set_p2y", valid_type=ParameterData,required=False)
        spec.input("calculation_set", valid_type=ParameterData )
        spec.input("parent_scf_folder", valid_type=RemoteData, required=False)
        spec.input("settings_p2y", valid_type=ParameterData, required=False, default = p2y_default_settings())
        spec.input("settings", valid_type=ParameterData, required=False, default = yambo_default_settings())
        spec.input("structure", valid_type=StructureData,required=False)
        spec.input("parent_nscf_folder", valid_type=RemoteData, required=False)
        spec.input("parameters_p2y", valid_type=ParameterData, required=False, default=set_default_qp_param() )
        spec.input("parameters", valid_type=ParameterData, required=False , default=set_default_qp_param() )
        spec.input("parameters_pw", valid_type=ParameterData, required=False   )
        spec.input("parameters_pw_nscf", valid_type=ParameterData, required=False   )
        spec.input("converge_parameters", valid_type=List)
        spec.input("starting_points", valid_type=List,required=False,default=List() )
        spec.input("default_step_size", valid_type=ParameterData,required=False,
                           default=DataFactory('parameter')(dict=default_step_size))
        spec.input("threshold", valid_type=Float, required=False, default=Float(0.1))

        spec.outline(
          cls.start,
          cls.iterate,
          while_(cls.is_not_converged)(
              cls.run_next_update,
              cls.iterate,
              ),
          cls.report_wf
        )
        spec.dynamic_output()


    def start(self):
        # for kpoints, we will need to have the scf step, and  use YamboWorkflow not YamboRestartWf
        # 
        #self.iterate([0,1,2,3])
        self.ctx.skip_prescf = False
        self.ctx.very_first = True
        self.ctx.iterate_count=1

    def init_parameters(self,paging):
        if 'calculation_set_pw' not in self.inputs.keys():
            self.inputs.calculation_set_pw = DataFactory('parameter')(dict=self.inputs.calculation_set.get_dict())
        if 'calculation_set_p2y' not in self.inputs.keys():
            main_set = self.inputs.calculation_set.get_dict()
            main_set['resources'] = {"num_machines": 1,"num_mpiprocs_per_machine":  1}
            self.inputs.calculation_set_p2y = DataFactory('parameter')(dict=main_set)

        self.ctx.conv_elem = {}
        self.ctx.en_diffs =  []
        params = {}
        if 'parameters' in self.inputs.keys():
            params = self.inputs.parameters.get_dict()
        if 'parameters' in self.inputs.keys():
            params = self.inputs.parameters.get_dict()
        if 'kpoints' not in self.inputs.converge_parameters:
            self.report(" kpoints not in self.inputs.converge_parameters")
            for idx in range(len(self.inputs.converge_parameters)):
                 starting_point = self.inputs.starting_points[idx] 
                 field =  self.inputs.converge_parameters[idx] # PPAPntXp,...
                 update_delta = np.ceil(self.inputs.default_step_size.get_dict()[field]*paging* starting_point )
                 params[ field ] = update_parameter_field( field, starting_point, update_delta)
                 self.report(" params now  {}".format(params) )
                 if field in self.ctx.conv_elem:
                     self.ctx.conv_elem[field].append(params[field])
                 else:
                     entry = []
                     entry.append(params[field])
                     self.ctx.conv_elem[field] = entry
        self.inputs.parameters = DataFactory('parameter')(dict= params)
        self.report( "params   {}  have been fully initialized for  {}".format(params, self.inputs.converge_parameters))

        if 'parent_scf_folder' in  self.inputs.keys(): 
            parent_calc = self.inputs.parent_scf_folder.get_inputs_dict(link_type=LinkType.CREATE)['remote_folder']
            if isinstance(parent_calc, PwCalculation):
                if parent_calc.get_state()== 'FINISHED': 
                    if 'settings_pw' not in self.inputs.keys():
                        self.inputs.settings_pw = parent_calc.inp.settings
                    if 'structure' not in self.inputs.keys():
                        self.inputs.structure = parent_calc.inp.structure
                    if 'pseudo' not in self.inputs.keys():
                        raise InputValidationError("Pseudo should be provided")
                    if 'parameters' not in self.inputs.keys():
                        self.report("setting default parameters ")
                        self.inputs.parameters = set_default_qp_param()
                    if parent_calc.get_inputs_dict(link_type=LinkType.CREATE)['parameters'].get_dict()['CONTROL']['calculation'] == 'scf':
                        if 'parameters_pw' not in self.inputs.keys():
                            self.inputs.parameters_pw = parent_calc.inp.parameters
                    else:
                        if 'parameters_pw_nscf' not in self.inputs.keys():
                            self.inputs.parameters_pw_nscf = parent_calc.inp.parameters

            if isinstance(parent_calc, YamboCalculation):
                if parent_calc.get_state()== 'FINISHED': 
                    if 'parameters' not in self.inputs.keys():
                        self.report("setting default parameters ")
                        self.inputs.parameters = set_default_qp_param()

            if 'kpoints' in self.inputs.converge_parameters:
                if 'settings_pw' not in self.inputs.keys():
                    self.inputs.settings_pw =  default_pw_settings() 
                if 'parameters_pw' not in self.inputs.keys():
                    self.inputs.parameters_pw = set_default_pw_param() 
                if 'parameters_pw_nscf' not in self.inputs.keys():
                    self.inputs.parameters_pw_nscf = set_default_pw_param(nscf=True) 
                if 'parameters' not in self.inputs.keys():
                    self.inputs.parameters = set_default_qp_param()
                      
        else:
            self.report(" initializing in a kpoints convergence calculation")
            if 'kpoints' in self.inputs.converge_parameters:
                if 'settings_pw' not in self.inputs.keys():
                    self.inputs.settings_pw =  default_pw_settings() 
                if 'parameters_pw' not in self.inputs.keys():
                    self.report("  parameters_pw were not found setting them to default pw params")
                    self.inputs.parameters_pw = set_default_pw_param() 
                if 'parameters_pw_nscf' not in self.inputs.keys():
                    self.report("  parameters_pw_nscf were not found setting them to default pw params")
                    self.inputs.parameters_pw_nscf = set_default_pw_param(nscf=True) 
                if 'parameters' not in self.inputs.keys():
                    self.inputs.parameters = set_default_qp_param()
            if 'structure' not in self.inputs.keys() :
                raise InputValidationError("Structure should be provided if parent PW SCF folder is not given when converging kpoints")
            if 'pseudo' not in self.inputs.keys():
                raise InputValidationError("Pseudo should be provided if parent PW calculation is not given when converging kpoints")
            if 'pwcode' not in self.inputs.keys():
                raise InputValidationError("PW code  should be provided when converging kpoints")
            if 'kpoints' not in self.inputs.converge_parameters and 'parent_nscf_folder' not in self.inputs.keys():
                raise InputValidationError("Parent nscf folder should be provided when not converging kpoints")
        if 'parent_nscf_folder' in  self.inputs.keys(): 
              parent_calc = self.inputs.parent_nscf_folder.get_inputs_dict()['remote_folder']
              if isinstance(parent_calc, PwCalculation):
                  if parent_calc.get_inputs_dict()['parameters'].get_dict()['CONTROL']['calculation'] == 'nscf'\
                       and parent_calc.get_state()== 'FINISHED' and 'QPkrange' not in self.inputs.parameters.get_dict().keys():
                      if 'parameters' not in self.inputs.keys():
                          self.inputs.parameters = set_default_qp_param()
                      self.inputs.parameters['QPkrange'] = default_qpkrange(parent_calc.pk, self.inputs.parameters)
        self.ctx.distance_kpoints = 0.2
        self.report('complete. ' )



    def run_next_update(self):
        if self.ctx.skip_prescf: 
            p2y_parent = load_node( self.ctx.missing_p2y_parent.out.gw.get_dict()["yambo_pk"])
            self.ctx.p2y_parent_folder = p2y_parent.out.remote_folder
            self.ctx.skip_prescf = False
        #self.iterate([1,2,3,4])

    def iterate(self):
        self.ctx.iterate_count+=1
        loop_items = [1,2,3,4]
        if self.ctx.very_first == True:
            loop_items = [0,1,2,3]
            self.ctx.very_first = False
        self.report(' will run four calculations first ' )
        outs={}
        if 'kpoints' not in self.inputs.converge_parameters:
            self.report(" this is not a K-point convergence ")
            for num in loop_items: # includes 0 because of starting point
                if loop_items[0] == 0:
                    self.init_parameters(num)
                else:
                    self.update_parameters(num)
                try: 
                    p2y_done = self.ctx.p2y_parent_folder 
                except AttributeError:
                    self.report('yamboconvergence.py:  iterate(): no preceeding yambo parent, will run P2Y from NSCF parent first ' )
                    p2y_res =  submit (YamboRestartWf,
                                precode= self.inputs.precode.copy(),
                                yambocode=self.inputs.yambocode.copy(),
                                parameters = self.inputs.parameters_p2y.copy(),
                                calculation_set= self.inputs.calculation_set_p2y.copy(),
                                parent_folder = self.inputs.parent_nscf_folder, settings = self.inputs.settings_p2y.copy()) 
                    self.ctx.skip_prescf = True
                    self.ctx.iterate_count-=1  
                    self.ctx.very_first = True
                    return ToContext(missing_p2y_parent= p2y_res)
                self.report(' running from preceeding yambo/p2y calculation  ' )
                future =  submit  (YamboRestartWf,
                            precode= self.inputs.precode.copy(),
                            yambocode=self.inputs.yambocode.copy(),
                            parameters = self.inputs.parameters.copy(),
                            calculation_set= self.inputs.calculation_set.copy(),
                            parent_folder = self.ctx.p2y_parent_folder, settings = self.inputs.settings.copy())
                outs[ 'r'+str(num) ] =  future
            self.report (" waiting  for result of YamboRestartWf ")
            return ToContext(**outs )
        else:
            # run yambowf, four times. with a different  nscf kpoint starting mesh
            self.report("  K-point convergence ")
            for num in loop_items: # includes 0 because of starting point
                if loop_items[0] == 0:
                    self.init_parameters(num)
                else:
                    self.update_parameters(num)
                self.ctx.distance_kpoints = self.ctx.distance_kpoints*1.5
                kpoints = KpointsData()
                kpoints.set_cell_from_structure(self.inputs.structure.copy())
                kpoints.set_kpoints_mesh_from_density(distance= self.ctx.distance_kpoints,force_parity=False)
                extra = {}
                if 'parent_scf_folder' in self.inputs.keys():
                   extra['parent_folder'] = self.inputs.parent_scf_folder
                if 'QPkrange' not in self.inputs.parameters.get_dict().keys():
                   extra['to_set_qpkrange'] = Bool(1)
                future =  submit (YamboWorkflow, codename_pw= self.inputs.pwcode.copy(), codename_p2y=self.inputs.precode.copy(),
                   codename_yambo= self.inputs.yambocode.copy(), pseudo_family= self.inputs.pseudo.copy(),
                   calculation_set_pw= self.inputs.calculation_set_pw.copy(),
                   calculation_set_p2y = self.inputs.calculation_set_p2y.copy(),
                   calculation_set_yambo = self.inputs.calculation_set.copy(),
                   settings_pw =self.inputs.settings_pw.copy(), settings_p2y = self.inputs.settings_p2y.copy(),
                   settings_yambo=self.inputs.settings.copy() , structure = self.inputs.structure.copy(),
                   kpoint_pw = kpoints, parameters_pw= self.inputs.parameters_pw.copy(), parameters_pw_nscf= self.inputs.parameters_pw_nscf.copy(),
                   parameters_p2y= self.inputs.parameters_p2y.copy(), parameters_yambo=  self.inputs.parameters.copy(),
                   **extra)
                outs[ 'r'+str(num) ] = future
            for num in loop_items: # includes 0 because of starting point
                self.report(" waiting  for result of YamboWorkflow ")
                #outs[ 'r'+str(num) ] = outs['r'+str(num)]
            return ToContext(**outs )  
        return outs 

    def interstep(self):
        self.report("interstep() ", self.ctx.r0)
        return

    def update_parameters(self, paging):
        try:
            params = self.inputs.parameters.get_dict()
            self.report("we had the parameters to update")
        except AttributeError:
            self.report("parameters is still empty.")
            params = set_default_qp_param().get_dict() 
        self.report("update parameters {}  and {} and {}".format(self.inputs.converge_parameters, params, paging ))
        for idx in range(len(self.inputs.converge_parameters)):
             starting_point = self.inputs.starting_points[idx]
             field = self.inputs.converge_parameters[idx]
             update_delta = np.ceil( self.inputs.default_step_size.get_dict()[field]*paging*starting_point * self.ctx.iterate_count) 
             params[field] = update_parameter_field( field, params[field] ,  update_delta ) 
             self.ctx.conv_elem[field].append(params[field])
        self.report(" extended convergence points: {}".format(self.ctx.conv_elem))
        self.inputs.parameters = DataFactory('parameter')(dict= params)


    def is_not_converged(self):
        if self.ctx.skip_prescf == True:
            return True 

        try: # for yamborestart
            r0_width = self.get_total_range(self.ctx.r0.out.gw.get_dict()['yambo_pk'])
            r1_width = self.get_total_range(self.ctx.r1.out.gw.get_dict()['yambo_pk'])
            r2_width = self.get_total_range(self.ctx.r2.out.gw.get_dict()['yambo_pk'])
            r3_width = self.get_total_range(self.ctx.r3.out.gw.get_dict()['yambo_pk'])
        except AttributeError: # for yamboworkflow
            r0_width = self.get_total_range(self.ctx.r1.out.gw.get_dict()['yambo_pk'])
            r1_width = self.get_total_range(self.ctx.r2.out.gw.get_dict()['yambo_pk'])
            r2_width = self.get_total_range(self.ctx.r3.out.gw.get_dict()['yambo_pk'])
            r3_width = self.get_total_range(self.ctx.r4.out.gw.get_dict()['yambo_pk'])

        self.ctx.en_diffs.extend([r0_width,r1_width,r2_width,r3_width])
        if 'scf_pk' in self.ctx.r3.out.gw.get_dict() and 'parent_scf_folder' not in self.inputs.keys():
            self.inputs.parent_scf_folder =  load_node(self.ctx.r3.out.gw.get_dict()['scf_pk']).out.remote_folder
        if len(self.ctx.en_diffs) > 16: # no more than 16 calcs
            self.report(" aborting after 16 calculations with no convergence ")
            return False
        if len(self.ctx.en_diffs) > 4:
            converged_fit = self.fitting_deviation(0) # only need to check against one convergence parameter at a time. 
            if  converged_fit:
                self.report("Convergence achieved. ")
                return False
            else:
                self.report("Convergence not yet achieved yet. ")
                return True
        else:
            # check the the  differences are minimal, when on first 4 calculations
            delta =  abs(r0_width-r1_width) + abs(r1_width-r2_width) + abs(r2_width-r3_width)
            if delta < self.inputs.threshold:
                return False
            else:
                return True
        return True

    def fitting_deviation(self,idx):
        field = self.inputs.converge_parameters[idx]
        independent = np.array(self.ctx.conv_elem[field])
        dependent = np.array(self.ctx.en_diffs)
        if len(independent)> 4:
            def func(x,a,b):
                y=1.0
                y =y*(a/x+b)
                return y
            popt,pcov = curve_fit(func,independent[:-4],dependent[:-4])
            a,b = popt
            fit_data = func(independent, a,b)      
            deviations = np.abs(dependent[:4] - fit_data[:4])  # deviation of last four from extrapolated values at those points
            converged_fit = np.allclose(deviations, np.linspace(0.01,0.01, 4), atol=self.inputs.threshold) # last four are within 0.01 of predicted value
            self.report("yamboconvergence.py: fitting_deviation: converged_fit {}".format(converged_fit))
            return converged_fit
        return False

    def get_total_range(self,node_id):
        # CAVEAT: this does not calculate HOMO LUMO gap, but the width between two
        #         bands listed in the QPkrange, on the first kpoint selected,
        #         i.e. for 'QPkrange': [(1,16,30,31)] , will find width between 
        #         kpoint 1  band 30 and kpoint  1 band 31. 
        calc = load_node(node_id)
        table=calc.out.array_qp.get_array('qp_table')
        try:
            qprange = calc.inp.parameters.get_dict()['QPkrange']
        except KeyError: 
            parent_calc =  calc.inp.parent_calc_folder.inp.remote_folder
            if isinstance(parent_calc, YamboCalculation):
                has_found_nelec = False
                while (not has_found_nelec):
                    try:
                        nelec = parent_calc.out.output_parameters.get_dict()['number_of_electrons']
                        has_found_nelec = True
                    except AttributeError:
                        parent_calc = parent_calc.inp.parent_calc_folder.inp.remote_folder
                    except KeyError:
                        parent_calc = parent_calc.inp.parent_calc_folder.inp.remote_folder
            elif isinstance(parent_calc, PwCalculation):
                nelec  = parent_calc.out.output_parameters.get_dict()['number_of_electrons']
            qprange =  [ ( 1, 1 , int(nelec*2) , int(nelec*2)+1 ) ]
        lowest_k = qprange[0][0] # first kpoint listed, 
        lowest_b = qprange[0][-2] # first band on first kpoint listed,
        highest_b= qprange[0][-1]  # last band on first kpoint listed,
        argwlk = np.argwhere(table[:,0]==float(lowest_k))  # indexes for lowest kpoint
        argwlb = np.argwhere(table[:,1]==float(lowest_b))  # indexes for lowest band
        argwhb = np.argwhere(table[:,1]==float(highest_b)) # indexes for highest band
        if len(argwlk)< 1:
            argwlk = np.array([0])
        if len(argwhb) < 1:
            argwhb = np.argwhere(table[:,1]== table[:,1][np.argmax(table[:,1])])
            argwlb = np.argwhere(table[:,1]== table[:,1][np.argmax(table[:,1])]-1 )
        arglb = np.intersect1d(argwlk,argwlb)              # index for lowest kpoints' lowest band
        arghb = np.intersect1d(argwlk,argwhb)              # index for lowest kpoint's highest band
        e_m_eo = calc.out.array_qp.get_array('E_minus_Eo') 
        eo = calc.out.array_qp.get_array('Eo')
        corrected = eo+e_m_eo
        corrected_lb = corrected[arglb]
        corrected_hb = corrected[arghb]
        self.report(" corrected gap(s)   at K-point {}, between bands {} and {}".format(
                    corrected_hb- corrected_lb, lowest_k, lowest_b, highest_b ))
        return (corrected_hb- corrected_lb)[0]  # for spin polarized there will be two almost equivalent, else just one value.

    def report_wf(self):
        """
        Output final quantities
        """
        extra = {}
        nscf_pk = False
        scf_pk = False
        parameters = None
        from aiida.orm import DataFactory
        if 'nscf_pk' in self.ctx.r1.out.gw.get_dict():
            nscf_pk = self.ctx.r1.out.gw.get_dict()['nscf_pk'] 
        if 'scf_pk' in self.ctx.r1.out.gw.get_dict():
            scf_pk = self.ctx.r1.out.gw.get_dict()['scf_pk'] 
        if 'yambo_pk' in self.ctx.r1.out.gw.get_dict():
            parameters = load_node( self.ctx.r1.out.gw.get_dict()['yambo_pk']).inp.parameters.get_dict()
        self.out("convergence", DataFactory('parameter')(dict={
            "parameters": parameters,
            "yambo_pk": self.ctx.r1.out.gw.get_dict()['yambo_pk'],
            "convergence_space": self.ctx.conv_elem,
            "energy_widths":  self.ctx.en_diffs ,
            "nscf_pk":  nscf_pk, 
            "scf_pk":  scf_pk , 
            }))

if __name__ == "__main__":
    pass
