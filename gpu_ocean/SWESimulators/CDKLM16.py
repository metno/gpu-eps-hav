# -*- coding: utf-8 -*-

"""
This software is part of GPU Ocean. 

Copyright (C) 2016 SINTEF ICT, 
Copyright (C) 2017-2019 SINTEF Digital
Copyright (C) 2017-2019 Norwegian Meteorological Institute

This python module implements the finite-volume scheme proposed by
Alina Chertock, Michael Dudzinski, A. Kurganov & Maria Lukacova-Medvidova (2016)
Well-Balanced Schemes for the Shallow Water Equations with Coriolis Forces

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

#Import packages we need
import numpy as np
import gc
import logging

from SWESimulators import Common, SimWriter, SimReader
from SWESimulators import Simulator
from SWESimulators import WindStress
from SWESimulators import OceanStateNoise

# Needed for the random perturbation of the wind forcing:
import pycuda.driver as cuda


class CDKLM16(Simulator.Simulator):
    """
    Class that solves the SW equations using the Coriolis well balanced reconstruction scheme, as given by the publication of Chertock, Dudzinski, Kurganov and Lukacova-Medvidova (CDFLM) in 2016.
    """

    def __init__(self, \
                 gpu_ctx, \
                 eta0, hu0, hv0, H, \
                 nx, ny, \
                 dx, dy, dt, \
                 g, f, r, \
                 t=0.0, \
                 theta=1.3, rk_order=2, \
                 coriolis_beta=0.0, \
                 y_zero_reference_cell = 0, \
                 max_wind_direction_perturbation = 0, \
                 wind_stress=WindStress.WindStress(), \
                 boundary_conditions=Common.BoundaryConditions(), \
                 small_scale_perturbation=False, \
                 small_scale_perturbation_amplitude=None, \
                 small_scale_perturbation_interpolation_factor = 1, \
                 model_time_step=None,
                 h0AsWaterElevation=False, \
                 reportGeostrophicEquilibrium=False, \
                 use_lcg=False, \
                 write_netcdf=False, \
                 netcdf_filename=None, \
                 ignore_ghostcells=False, \
                 offset_x=0, offset_y=0, \
                 block_width=32, block_height=8, num_threads_dt=256,
                 block_width_model_error=16, block_height_model_error=16):
        """
        Initialization routine
        eta0: Initial deviation from mean sea level incl ghost cells, (nx+2)*(ny+2) cells
        hu0: Initial momentum along x-axis incl ghost cells, (nx+1)*(ny+2) cells
        hv0: Initial momentum along y-axis incl ghost cells, (nx+2)*(ny+1) cells
        H: Depth from equilibrium defined on cell corners, (nx+5)*(ny+5) corners
        nx: Number of cells along x-axis
        ny: Number of cells along y-axis
        dx: Grid cell spacing along x-axis (20 000 m)
        dy: Grid cell spacing along y-axis (20 000 m)
        dt: Size of each timestep (90 s)
        g: Gravitational accelleration (9.81 m/s^2)
        f: Coriolis parameter (1.2e-4 s^1), effectively as f = f + beta*y
        r: Bottom friction coefficient (2.4e-3 m/s)
        t: Start simulation at time t
        theta: MINMOD theta used the reconstructions of the derivatives in the numerical scheme
        rk_order: Order of Runge Kutta method {1,2*,3}
        coriolis_beta: Coriolis linear factor -> f = f + beta*(y-y_0)
        y_zero_reference_cell: The cell representing y_0 in the above, defined as the lower face of the cell .
        max_wind_direction_perturbation: Large-scale model error emulation by per-time-step perturbation of wind direction by +/- max_wind_direction_perturbation (degrees)
        wind_stress: Wind stress parameters
        boundary_conditions: Boundary condition object
        small_scale_perturbation: Boolean value for applying a stochastic model error
        small_scale_perturbation_amplitude: Amplitude (q0 coefficient) for model error
        small_scale_perturbation_interpolation_factor: Width factor for correlation in model error
        model_time_step: The size of a data assimilation model step (default same as dt)
        h0AsWaterElevation: True if h0 is described by the surface elevation, and false if h0 is described by water depth
        reportGeostrophicEquilibrium: Calculate the Geostrophic Equilibrium variables for each superstep
        use_lcg: Use LCG as the random number generator. Default is False, which means using curand.
        write_netcdf: Write the results after each superstep to a netCDF file
        netcdf_filename: Use this filename. (If not defined, a filename will be generated by SimWriter.)
        """
               
        self.logger = logging.getLogger(__name__)

        ## After changing from (h, B) to (eta, H), several of the simulator settings used are wrong. This check will help detect that.
        if ( np.sum(eta0 - H[:-1, :-1] > 0) > nx):
            assert(False), "It seems you are using water depth/elevation h and bottom topography B, while you should use water level eta and equillibrium depth H."
        
        assert( rk_order < 4 or rk_order > 0 ), "Only 1st, 2nd and 3rd order Runge Kutta supported"

        if (rk_order == 3):
            assert(r == 0.0), "3rd order Runge Kutta supported only without friction"
        
        # Sort out internally represented ghost_cells in the presence of given
        # boundary conditions
        ghost_cells_x = 2
        ghost_cells_y = 2
        y_zero_reference_cell = 2 + y_zero_reference_cell
        
        # Boundary conditions
        self.boundary_conditions = boundary_conditions
        if (boundary_conditions.isSponge()):
            nx = nx + boundary_conditions.spongeCells[1] + boundary_conditions.spongeCells[3] - 2*ghost_cells_x
            ny = ny + boundary_conditions.spongeCells[0] + boundary_conditions.spongeCells[2] - 2*ghost_cells_y
            y_zero_reference_cell = boundary_conditions.spongeCells[2] + y_zero_reference_cell
        
        A = None
        self.max_wind_direction_perturbation = max_wind_direction_perturbation
        super(CDKLM16, self).__init__(gpu_ctx, \
                                      nx, ny, \
                                      ghost_cells_x, \
                                      ghost_cells_y, \
                                      dx, dy, dt, \
                                      g, f, r, A, \
                                      t, \
                                      theta, rk_order, \
                                      coriolis_beta, \
                                      y_zero_reference_cell, \
                                      wind_stress, \
                                      write_netcdf, \
                                      ignore_ghostcells, \
                                      offset_x, offset_y, \
                                      block_width, block_height)
        
        # Index range for interior domain (north, east, south, west)
        # so that interior domain of eta is
        # eta[self.interior_domain_indices[2]:self.interior_domain_indices[0], \
        #     self.interior_domain_indices[3]:self.interior_domain_indices[1] ]
        self.interior_domain_indices = np.array([-2,-2,2,2])
        self._set_interior_domain_from_sponge_cells()
        
        #Get kernels
        self.kernel = gpu_ctx.get_kernel("CDKLM16_kernel.cu", 
                defines={'block_width': block_width, 'block_height': block_height}, 
                compile_args={                          # default, fast_math, optimal
                    'options' : ["--ftz=true",          # false,   true,      true
                                 "--prec-div=false",    # true,    false,     false,
                                 "--prec-sqrt=false",   # true,    false,     false
                                 "--fmad=false"]        # true,    true,      false
                    
                    #'options': ["--use_fast_math"]
                    #'options': ["--generate-line-info"], 
                    #nvcc_options=["--maxrregcount=39"],
                    #'arch': "compute_50", 
                    #'code': "sm_50"
                },
                jit_compile_args={
                    #jit_options=[(cuda.jit_option.MAX_REGISTERS, 39)]
                }
                )
        
        # Get CUDA functions and define data types for prepared_{async_}call()
        self.cdklm_swe_2D = self.kernel.get_function("cdklm_swe_2D")
        self.cdklm_swe_2D.prepare("iifffffffffiiPiPiPiPiPiPiPiPifi")
        self.update_wind_stress(self.kernel, self.cdklm_swe_2D)
        
        # CUDA functions for finding max time step size:
        self.num_threads_dt = num_threads_dt
        self.num_blocks_dt  = np.int32(self.global_size[0]*self.global_size[1])
        self.update_dt_kernels = gpu_ctx.get_kernel("max_dt.cu",
                defines={'block_width': block_width, 
                         'block_height': block_height,
                         'NUM_THREADS': self.num_threads_dt})
        self.per_block_max_dt_kernel = self.update_dt_kernels.get_function("per_block_max_dt")
        self.per_block_max_dt_kernel.prepare("iifffPiPiPiPiPi")
        self.max_dt_reduction_kernel = self.update_dt_kernels.get_function("max_dt_reduction")
        self.max_dt_reduction_kernel.prepare("iPP")
        
        #Create data by uploading to device
        self.gpu_data = Common.SWEDataArakawaA(self.gpu_stream, nx, ny, ghost_cells_x, ghost_cells_y, eta0, hu0, hv0)

        # Allocate memory for calculating maximum timestep
        host_dt = np.zeros((self.global_size[1], self.global_size[0]), dtype=np.float32)
        self.device_dt = Common.CUDAArray2D(self.gpu_stream, self.global_size[0], self.global_size[1],
                                            0, 0, host_dt)
        host_max_dt_buffer = np.zeros((1,1), dtype=np.float32)
        self.max_dt_buffer = Common.CUDAArray2D(self.gpu_stream, 1, 1, 0, 0, host_max_dt_buffer)
        
        
        ## Allocating memory for geostrophical equilibrium variables
        self.reportGeostrophicEquilibrium = np.int32(reportGeostrophicEquilibrium)
        self.geoEq_uxpvy = None
        self.geoEq_Kx = None
        self.geoEq_Ly = None
        if self.reportGeostrophicEquilibrium:
            dummy_zero_array = np.zeros((ny+2*ghost_cells_y, nx+2*ghost_cells_x), dtype=np.float32, order='C') 
            self.geoEq_uxpvy = Common.CUDAArray2D(self.gpu_stream, nx, ny, ghost_cells_x, ghost_cells_y, dummy_zero_array)
            self.geoEq_Kx = Common.CUDAArray2D(self.gpu_stream, nx, ny, ghost_cells_x, ghost_cells_y, dummy_zero_array)
            self.geoEq_Ly = Common.CUDAArray2D(self.gpu_stream, nx, ny, ghost_cells_x, ghost_cells_y, dummy_zero_array)

        #Bathymetry
        self.bathymetry = Common.Bathymetry(gpu_ctx, self.gpu_stream, nx, ny, ghost_cells_x, ghost_cells_y, H, boundary_conditions)
        self.h0AsWaterElevation = h0AsWaterElevation
        if self.h0AsWaterElevation:
            self.bathymetry.waterElevationToDepth(self.gpu_data.h0)
        
        self.constant_equilibrium_depth = np.max(H)
        
        self.bc_kernel = Common.BoundaryConditionsArakawaA(gpu_ctx, \
                                                           self.nx, \
                                                           self.ny, \
                                                           ghost_cells_x, \
                                                           ghost_cells_y, \
                                                           self.boundary_conditions, \
        )

        # Small scale perturbation:
        self.small_scale_perturbation = small_scale_perturbation
        self.small_scale_model_error = None
        self.small_scale_perturbation_interpolation_factor = small_scale_perturbation_interpolation_factor
        if small_scale_perturbation:
            if small_scale_perturbation_amplitude is None:
                self.small_scale_model_error = OceanStateNoise.OceanStateNoise.fromsim(self,
                                                                                       interpolation_factor=small_scale_perturbation_interpolation_factor,
                                                                                       use_lcg=use_lcg,
                                                                                       block_width=block_width_model_error, 
                                                                                       block_height=block_height_model_error)
            else:
                self.small_scale_model_error = OceanStateNoise.OceanStateNoise.fromsim(self, 
                                                                                       soar_q0=small_scale_perturbation_amplitude,
                                                                                       interpolation_factor=small_scale_perturbation_interpolation_factor,
                                                                                       use_lcg=use_lcg,
                                                                                       block_width=block_width_model_error, 
                                                                                       block_height=block_height_model_error)
        
        # Data assimilation model step size
        self.model_time_step = model_time_step
        if model_time_step is None:
            self.model_time_step = self.dt
        self.total_time_steps = 0
        
        
        if self.write_netcdf:
            self.sim_writer = SimWriter.SimNetCDFWriter(self, filename=netcdf_filename, ignore_ghostcells=self.ignore_ghostcells, \
                                    offset_x=self.offset_x, offset_y=self.offset_y)

    
    def cleanUp(self):
        """
        Clean up function
        """
        self.closeNetCDF()
        
        self.gpu_data.release()
        
        if self.small_scale_model_error is not None:
            self.small_scale_model_error.cleanUp()
        
        
        if self.geoEq_uxpvy is not None:
            self.geoEq_uxpvy.release()
        if self.geoEq_Kx is not None:
            self.geoEq_Kx.release()
        if self.geoEq_Ly is not None:
            self.geoEq_Ly.release()
        self.bathymetry.release()
        self.h0AsWaterElevation = False # Quick fix to stop waterDepthToElevation conversion
        
        self.device_dt.release()
        self.max_dt_buffer.release()
        
        self.gpu_ctx = None
        gc.collect()
           
    @classmethod
    def fromfilename(cls, gpu_ctx, filename, cont_write_netcdf=True, use_lcg=False, new_netcdf_filename=None):
        """
        Initialize and hotstart simulation from nc-file.
        cont_write_netcdf: Continue to write the results after each superstep to a new netCDF file
        filename: Continue simulation based on parameters and last timestep in this file
        new_netcdf_filename: If we want to continue to write netcdf, we should use this filename. Automatically generated if None.
        """
        # open nc-file
        sim_reader = SimReader.SimNetCDFReader(filename, ignore_ghostcells=False)
        sim_name = str(sim_reader.get('simulator_short'))
        assert sim_name == cls.__name__, \
               "Trying to initialize a " + \
               cls.__name__ + " simulator with netCDF file based on " \
               + sim_name + " results."
        
        # read the most recent state 
        H = sim_reader.getH();
        
        # get last timestep (including simulation time of last timestep)
        eta0, hu0, hv0, time0 = sim_reader.getLastTimeStep()
        
        # For some reason, some old netcdf had 3-dimensional bathymetry.
        # This fix ensures that we only use a valid H
        if len(H.shape) == 3:
            print("norm diff H: ", np.linalg.norm(H[0,:,:] - H[1,:,:]))
            H = H[0,:,:]
       
        # Set simulation parameters
        sim_params = {
            'gpu_ctx': gpu_ctx,
            'eta0': eta0,
            'hu0': hu0,
            'hv0': hv0,
            'H': H,
            'nx': sim_reader.get("nx"), 
            'ny': sim_reader.get("ny"),
            'dx': sim_reader.get("dx"),
            'dy': sim_reader.get("dy"),
            'dt': sim_reader.get("dt"),
            'g': sim_reader.get("g"),
            'f': sim_reader.get("coriolis_force"),
            'r': sim_reader.get("bottom_friction_r"),
            't': time0,
            'theta': sim_reader.get("minmod_theta"),
            'rk_order': sim_reader.get("time_integrator"),
            'coriolis_beta': sim_reader.get("coriolis_beta"),
            'y_zero_reference_cell': sim_reader.get("y_zero_reference_cell"),
            'write_netcdf': cont_write_netcdf,
            'use_lcg': use_lcg,
            'netcdf_filename': new_netcdf_filename
        }    
        
        # Wind stress
        try:
            wind_stress_type = sim_reader.get("wind_stress_type")
            wind = Common.WindStressParams(type=wind_stress_type)
        except:
            wind = WindStress.WindStress()
        sim_params['wind_stress'] = wind
            
        # Boundary conditions
        sim_params['boundary_conditions'] = Common.BoundaryConditions( \
            sim_reader.getBC()[0], sim_reader.getBC()[1], \
            sim_reader.getBC()[2], sim_reader.getBC()[3], \
            sim_reader.getBCSpongeCells())
    
        # Model errors
        if sim_reader.has('small_scale_perturbation'):
            sim_params['small_scale_perturbation'] = sim_reader.get('small_scale_perturbation') == 'True'
            if sim_params['small_scale_perturbation']:
                sim_params['small_scale_perturbation_amplitude'] = sim_reader.get('small_scale_perturbation_amplitude')
                sim_params['small_scale_perturbation_interpolation_factor'] = sim_reader.get('small_scale_perturbation_interpolation_factor')
            
            
        # Data assimilation parameters:
        if sim_reader.has('model_time_step'):
            sim_params['model_time_step'] = sim_reader.get('model_time_step')
    
        return cls(**sim_params)
    
    
    
    def step(self, t_end=0.0, apply_stochastic_term=True, write_now=True):
        """
        Function which steps n timesteps.
        apply_stochastic_term: Boolean value for whether the stochastic
            perturbation (if any) should be applied.
        """
        n = int(t_end / self.dt + 1)

        if self.t == 0:
            self.bc_kernel.boundaryCondition(self.gpu_stream, \
                                             self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0)
        
        for i in range(0, n):
            # Get new random wind direction (emulationg large-scale model error)
            if(self.max_wind_direction_perturbation > 0.0 and self.wind_stress.type() == 1):
                # max perturbation +/- max_wind_direction_perturbation deg within original wind direction (at t=0)
                perturbation = 2.0*(np.random.rand()-0.5) * self.max_wind_direction_perturbation;
                new_wind_stress = WindStress.GenericUniformWindStress( \
                    rho_air=self.wind_stress.rho_air, \
                    wind_speed=self.wind_stress.wind_speed, \
                    wind_direction=self.wind_stress.wind_direction + perturbation)
                # Upload new wind stress params to device
                cuda.memcpy_htod_async(int(self.wind_stress_dev), new_wind_stress.tostruct(), stream=self.gpu_stream)
                
            local_dt = np.float32(min(self.dt, t_end-i*self.dt))
            
            if (local_dt <= 0.0):
                break
            
            wind_stress_t = np.float32(self.update_wind_stress(self.kernel, self.cdklm_swe_2D))

            #self.bc_kernel.boundaryCondition(self.cl_queue, \
            #            self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1)
            
            # 2nd order Runge Kutta
            if (self.rk_order == 2):

                self.callKernel(self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                local_dt, wind_stress_t, 0)

                self.bc_kernel.boundaryCondition(self.gpu_stream, \
                        self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1)

                self.callKernel(self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                local_dt, wind_stress_t, 1)

                # Applying final boundary conditions after perturbation (if applicable)
                
            elif (self.rk_order == 1):
                self.callKernel(self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                local_dt, wind_stress_t, 0)
                                
                self.gpu_data.swap()

                # Applying boundary conditions after perturbation (if applicable)
                
            # 3rd order RK method:
            elif (self.rk_order == 3):

                self.callKernel(self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                local_dt, wind_stress_t, 0)
                
                self.bc_kernel.boundaryCondition(self.gpu_stream, \
                        self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1)

                self.callKernel(self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                local_dt, wind_stress_t, 1)

                self.bc_kernel.boundaryCondition(self.gpu_stream, \
                        self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1)

                self.callKernel(self.gpu_data.h1, self.gpu_data.hu1, self.gpu_data.hv1, \
                                self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0, \
                                local_dt, wind_stress_t, 2)
                
                # Applying final boundary conditions after perturbation (if applicable)
            
            # Perturb ocean state with model error
            if self.small_scale_perturbation and apply_stochastic_term:
                self.small_scale_model_error.perturbSim(self)
                
            # Apply boundary conditions
            self.bc_kernel.boundaryCondition(self.gpu_stream, \
                        self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0)
            
            # Evolve drifters
            if self.hasDrifters:
                self.drifters.drift(self.gpu_data.h0, self.gpu_data.hu0, \
                                    self.gpu_data.hv0, \
                                    np.float32(self.constant_equilibrium_depth), \
                                    self.nx, self.ny, self.dx, self.dy, \
                                    local_dt, \
                                    np.int32(2), np.int32(2))
            self.t += np.float64(local_dt)
            self.num_iterations += 1
            
        if self.write_netcdf and write_now:
            self.sim_writer.writeTimestep(self)
            
        return self.t


    def callKernel(self, \
                   h_in, hu_in, hv_in, \
                   h_out, hu_out, hv_out, \
                   local_dt, wind_stress_t, rk_step):
            
        #"Beautify" code a bit by packing four bools into a single int
        #Note: Must match code in kernel!
        boundary_conditions = np.int32(0)
        if (self.boundary_conditions.north == 1):
            boundary_conditions = boundary_conditions | 0x01
        if (self.boundary_conditions.east == 1):
            boundary_conditions = boundary_conditions | 0x02
        if (self.boundary_conditions.south == 1):
            boundary_conditions = boundary_conditions | 0x04
        if (self.boundary_conditions.west == 1):
            boundary_conditions = boundary_conditions | 0x08
            
        self.cdklm_swe_2D.prepared_async_call(self.global_size, self.local_size, self.gpu_stream, \
                           self.nx, self.ny, \
                           self.dx, self.dy, local_dt, \
                           self.g, \
                           self.theta, \
                           self.f, \
                           self.coriolis_beta, \
                           self.y_zero_reference_cell, \
                           self.r, \
                           self.rk_order, \
                           np.int32(rk_step), \
                           h_in.data.gpudata, h_in.pitch, \
                           hu_in.data.gpudata, hu_in.pitch, \
                           hv_in.data.gpudata, hv_in.pitch, \
                           h_out.data.gpudata, h_out.pitch, \
                           hu_out.data.gpudata, hu_out.pitch, \
                           hv_out.data.gpudata, hv_out.pitch, \
                           self.bathymetry.Bi.data.gpudata, self.bathymetry.Bi.pitch, \
                           self.bathymetry.Bm.data.gpudata, self.bathymetry.Bm.pitch, \
                           wind_stress_t, \
                           boundary_conditions)
            
    
    def perturbState(self, q0_scale=1):
        self.small_scale_model_error.perturbSim(self, q0_scale=q0_scale)
    
    def applyBoundaryConditions(self):
        self.bc_kernel.boundaryCondition(self.gpu_stream, \
                        self.gpu_data.h0, self.gpu_data.hu0, self.gpu_data.hv0)
    
    def dataAssimilationStep(self, observation_time, model_error_final_step=True, write_now=True, courant_number=0.8):
        """
        The model runs until self.t = observation_time - self.model_time_step with model error.
        If model_error_final_step is true, another stochastic model_time_step is performed, 
        otherwise a deterministic model_time_step.
        """
        # For the IEWPF scheme, it is important that the final timestep before the
        # observation time is a full time step (fully deterministic). 
        # We therefore make sure to take the (potential) small timestep first in this function,
        # followed by appropriately many full time steps.
        
        full_model_time_steps = int(round(observation_time - self.t)/self.model_time_step)
        leftover_step_size = observation_time - self.t - full_model_time_steps*self.model_time_step

        # Avoid a too small extra timestep
        if leftover_step_size/self.model_time_step < 0.1 and full_model_time_steps > 1:
            leftover_step_size += self.model_time_step
            full_model_time_steps -= 1
        
        # Force leftover_step_size to zero if it is very small compared to the model_time_step
        if leftover_step_size/self.model_time_step < 0.00001:
            leftover_step_size = 0

        assert(full_model_time_steps > 0), "There is less than CDKLM16.model_time_step until the observation"

        # Start by updating the timestep size.
        self.updateDt(courant_number=courant_number)
            
        # Loop standard steps:
        for i in range(full_model_time_steps+1):
            
            if i == 0 and leftover_step_size == 0:
                continue
            elif i == 0:
                # Take the leftover step
                self.step(leftover_step_size, apply_stochastic_term=False, write_now=False)
                self.perturbState(q0_scale=np.sqrt(leftover_step_size/self.model_time_step))

            else:
                # Take standard steps
                self.step(self.model_time_step, apply_stochastic_term=False, write_now=False)
                if (i < full_model_time_steps) or model_error_final_step:
                    self.perturbState()
                    
            self.total_time_steps += 1
            
            # Update dt now and then
            if self.total_time_steps % 5 == 0:
                self.updateDt(courant_number=courant_number)
            
        if self.write_netcdf and write_now:
            self.sim_writer.writeTimestep(self)
    
        assert(round(observation_time) == round(self.t)), 'The simulation time is not the same as observation time after dataAssimilationStep! \n' + \
            '(self.t, observation_time, diff): ' + str((self.t, observation_time, self.t - observation_time))
    
    def writeState(self):        
        if self.write_netcdf:
            self.sim_writer.writeTimestep(self)
        
    
    
    def updateDt(self, courant_number=0.8):
        """
        Updates the time step self.dt by finding the maximum size of dt according to the 
        CFL conditions, and scale it with the provided courant number (0.8 on default).
        """
        self.per_block_max_dt_kernel.prepared_async_call(self.global_size, self.local_size, self.gpu_stream, \
                   self.nx, self.ny, \
                   self.dx, self.dy, \
                   self.g, \
                   self.gpu_data.h0.data.gpudata, self.gpu_data.h0.pitch, \
                   self.gpu_data.hu0.data.gpudata, self.gpu_data.hu0.pitch, \
                   self.gpu_data.hv0.data.gpudata, self.gpu_data.hv0.pitch, \
                   self.bathymetry.Bm.data.gpudata, self.bathymetry.Bm.pitch, \
                   self.device_dt.data.gpudata, self.device_dt.pitch)
    
        self.max_dt_reduction_kernel.prepared_async_call((1,1),
                                                         (self.num_threads_dt,1,1),
                                                         self.gpu_stream,
                                                         self.num_blocks_dt,
                                                         self.device_dt.data.gpudata,
                                                         self.max_dt_buffer.data.gpudata)

        dt_host = self.max_dt_buffer.download(self.gpu_stream)
        self.dt = courant_number*dt_host[0,0]
    
    def _getMaxTimestepHost(self, courant_number=0.8):
        """
        Calculates the maximum allowed time step according to the CFL conditions and scales the
        result with the provided courant number (0.8 on default).
        This function is for reference only, and suboptimally implemented on the host.
        """
        eta, hu, hv = self.download(interior_domain_only=True)
        Hm = self.downloadBathymetry()[1][2:-2, 2:-2]
        #print(eta.shape, Hm.shape)
        
        h = eta + Hm
        gravityWaves = np.sqrt(self.g*h)
        u = hu/h
        v = hv/h
        
        max_dt = 0.25*min(self.dx/np.max(np.abs(u)+gravityWaves), 
                          self.dy/np.max(np.abs(v)+gravityWaves) )
        
        return courant_number*max_dt    
    
    def downloadBathymetry(self):
        return self.bathymetry.download(self.gpu_stream)
    
    def downloadDt(self):
        return self.device_dt.download(self.gpu_stream)

    def downloadGeoEqNorm(self):
        
        uxpvy_cpu = self.geoEq_uxpvy.download(self.gpu_stream)
        Kx_cpu = self.geoEq_Kx.download(self.gpu_stream)
        Ly_cpu = self.geoEq_Ly.download(self.gpu_stream)

        uxpvy_norm = np.linalg.norm(uxpvy_cpu)
        Kx_norm = np.linalg.norm(Kx_cpu)
        Ly_norm = np.linalg.norm(Ly_cpu)

        return uxpvy_norm, Kx_norm, Ly_norm