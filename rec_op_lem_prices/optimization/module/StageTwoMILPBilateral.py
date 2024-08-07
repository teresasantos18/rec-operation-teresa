"""
Class for implementing and running the Stage 2 MILP for an energy community.
The implementation is specific to a p2p structure, based on bilateral contracts.
"""
import itertools
import os
import re

from rec_op_lem_prices.configs.configs import (
    MIPGAP,
    SOLVER,
    TIMEOUT
)
from rec_op_lem_prices.optimization.helpers.milp_helpers import (
    dict_none_lists,
    dict_per_param,
    none_lists,
    round_up,
    time_intervals
)
from rec_op_lem_prices.custom_types.stage_two_milp_bilateral_types import (
    BackpackS2BilateralDict,
    OutputsS2BilateralDict
)
from loguru import logger
from pulp import (
    LpBinary,
    LpMinimize,
    LpProblem,
    LpStatus,
    lpSum,
    LpVariable,
    GUROBI_CMD,
    pulp,
    value
)


class StageTwoMILPBilateral:
    def __init__(self, backpack: BackpackS2BilateralDict, solver=SOLVER, timeout=TIMEOUT, mipgap=MIPGAP):
        # Indices and sets
        self._horizon = backpack.get('horizon')  # operation period (hours)
        # Parameters
        self._delta_t = backpack.get('delta_t')  # interval settlement duration [h]
        self._l_buy = None  # supply energy tariff [€/kWh]
        self._l_sell = None  # feed in energy tariff [€/kWh]
        self._l_market_buy = backpack.get('l_market_buy')  # market-indexed buying tariff [€/kWh]
        self._l_market_sell = backpack.get('l_market_sell')  # market-indexed selling tariff [€/kWh]
        self._e_c = None  # Meter load profile [kWh]
        self._e_g = None  # Meter generation [kWh]
        self._p_meter_max = None  # power flow limit at the Meter [kW]
        self._eff_bc = {}  # charging efficiency of the batteries [%]
        self._eff_bd = {}  # discharging efficiency of batteries [%]
        self._p_max = {}  # maximum input and output batteries' power [kW]
        self._e_bn = {}  # nominal capacity of the batteries'[kWh]
        self._soc_min = {}  # minimum state of charge of the batteries [%]
        self._soc_max = {}  # maximum state of charge of the batteries [%]
        self._init_e_bat = {}  # initial energy content of the batteries [kWh]
        self._deg_cost = {}  # estimated degradation cost of the batteries of n [€/kWh]
        self._c_ind = None  # objective function values of each Meters' 1st stage MILP solution
        self._l_grid = backpack.get('l_grid')  # access tariff of the local grid between each pair of members [€/kWh]
        self._l_lem = backpack.get('l_lem')  # price for LEM transactions [€/kWh]
        self._big_m = None  # a very big number [kWh]
        self._l_extra = backpack.get('l_extra')  # (fictitious) very high cost of violating p_meter_max
        self.annual_income = {'Meter#1': 134000, 'Meter#2': 49000, 'Meter#3': 102500, 'Meter#4': 13100, 'Meter#5': 35000}
        self.energy_exp = {'Meter#1': 4550, 'Meter#2': 2200, 'Meter#3': 4300, 'Meter#4': 1500, 'Meter#5': 1650}
        self.n_ind = {'Meter#1': 2, 'Meter#2': 1, 'Meter#3': 2, 'Meter#4': 2, 'Meter#5': 1}
        self.n_ind_no_inc = {'Meter#1': 2, 'Meter#2': 0, 'Meter#3': 3, 'Meter#4': 2, 'Meter#5': 0}
        self.epi = {}
        self.epi_cont = {}
        self.norm_1 = {}
        self.norm_2 = {}
        self.norm_3 = {}
        self.nvi = {}
        self.method = 2
        # MILP variables
        self.milp = None  # for storing the MILP formulation
        self.solver = solver  # solver chosen for the MILP
        self.timeout = timeout  # solvers temporal limit to find optimal solution (s)
        self.mipgap = mipgap  # controls the solver's tolerance; intolerant [0 - 1] fully permissive
        self.status = None  # stores the status of the MILP's solution
        self.obj_value = None  # stores the MILP's numeric solution
        self.time_intervals = None  # for number of time intervals per horizon
        self.time_series = None  # for a range of time intervals
        self.set_meters = None  # set with Meters' ID
        self.sets_btm_storage = {}  # stores the Meter's Btm storage assets' ids
        self._meters_data = backpack.get('meters')  # data from Meters
        self.second_stage = backpack.get('second_stage')  # indicates if second stage (True) or single stage (False)
        self.strict_pos_coeffs = backpack.get('strict_pos_coeffs')  # no negative coefficients if True
        self.total_share_coeffs = backpack.get('total_share_coeffs')  # share all required in the REC if True

    def __define_milp(self):
        """
        Method to define the second stage MILP problem.
        """
        logger.debug(f'-- defining the collective (bilateral) MILP problem...')

        # Define a minimization MILP
        self.milp = LpProblem(f'stage2', LpMinimize)

        # Additional temporal variables
        self.time_intervals = time_intervals(self._horizon, self._delta_t)
        self.time_series = range(self.time_intervals)

        # Set of Meters
        self.set_meters = list(self._meters_data.keys())
        self.sets_other_meters = {n: [m for m in self.set_meters if m != n] for n in self.set_meters}

        # For simplicity, unpack Meters' information into lists, by type of data, where each Meter is solely
        # identified by its relative position on the list
        self._l_buy = dict_per_param(self._meters_data, 'l_buy')
        self._l_sell = dict_per_param(self._meters_data, 'l_sell')
        self._e_c = dict_per_param(self._meters_data, 'e_c')
        self._e_g = dict_per_param(self._meters_data, 'e_g')
        self._p_meter_max = dict_per_param(self._meters_data, 'max_p')
        self._big_m = 2 * max(self._p_meter_max.values())
        if self.second_stage:
            self._c_ind = dict_per_param(self._meters_data, 'c_ind')
        else:
            # Unbound the restriction regarding stage 1 cost for single stage runs
            self._c_ind = {k: 1000 for k in self.set_meters}

        print('c_ind_stage1:', self._c_ind)

        # Unpack batterias information
        for n in self.set_meters:
            meter_btm_storage = self._meters_data[n]['btm_storage']
            if meter_btm_storage is not None:
                self.sets_btm_storage[n] = list(meter_btm_storage.keys())
                self._eff_bc[n] = {b: meter_btm_storage[b]['eff_bc'] for b in self.sets_btm_storage[n]}
                self._eff_bd[n] = {b: meter_btm_storage[b]['eff_bd'] for b in self.sets_btm_storage[n]}
                self._p_max[n] = {b: meter_btm_storage[b]['p_max'] for b in self.sets_btm_storage[n]}
                self._e_bn[n] = {b: meter_btm_storage[b]['e_bn'] for b in self.sets_btm_storage[n]}
                self._soc_min[n] = {b: meter_btm_storage[b]['soc_min'] for b in self.sets_btm_storage[n]}
                self._soc_max[n] = {b: meter_btm_storage[b]['soc_max'] for b in self.sets_btm_storage[n]}
                self._init_e_bat[n] = {b: meter_btm_storage[b]['init_e'] for b in self.sets_btm_storage[n]}
                self._deg_cost[n] = {b: meter_btm_storage[b]['degradation_cost'] for b in self.sets_btm_storage[n]}
            else:
                self.sets_btm_storage[n] = []

        # Initialize the decision variables
        # energy supplied to n from its retailer [kWh]
        e_sup_retail = dict_none_lists(self.time_intervals, self.set_meters)
        # energy surplus sold by n to its retailer [kWh]
        e_sur_retail = dict_none_lists(self.time_intervals, self.set_meters)
        # energy supplied to n at a market-indexed price [kWh]
        e_sup_market = dict_none_lists(self.time_intervals, self.set_meters)
        # energy surplus sold by n at a market-indexed price [kWh]
        e_sur_market = dict_none_lists(self.time_intervals, self.set_meters)
        # when True allows supply when false allows surplus
        delta_sup = dict_none_lists(self.time_intervals, self.set_meters)
        # energy bought locally by n from m
        e_pur = {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        # energy sold locally by n to m
        e_sale = {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        # net consumption at meter n [kWh]
        e_cmet = dict_none_lists(self.time_intervals, self.set_meters)
        # energy self-consumed by n, allocated from m (in theory, g.t.e. that value)
        e_slc = {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        # consumption at meter n (in theory, g.t.e. that value)
        e_consumed = dict_none_lists(self.time_intervals, self.set_meters)
        # consumed energy bought locally by n (in theory, g.t.e. that value)
        e_alc = dict_none_lists(self.time_intervals, self.set_meters)
        # auxiliary binary variable for defining self-consumed energy by n
        delta_slc = dict_none_lists(self.time_intervals, self.set_meters)
        # for defining e_consumed when a particular self._l_grid[t] is negative
        delta_cmet = dict_none_lists(self.time_intervals, self.set_meters)
        # for defining e_alc when a particular self._l_grid[t] is negative
        delta_alc = dict_none_lists(self.time_intervals, self.set_meters)
        # extra power flow at n, beyond p_meter_max [kW]
        p_extra = dict_none_lists(self.time_intervals, self.set_meters)
        # energy stored by the batteries of n [kWh]
        e_bat = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        # SOC of the batteries of n [%]
        soc_bat = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        # energy charged by n's batteries [kWh]
        e_bc = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        # energy discharged by n's batteries [kWh]
        e_bd = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        # when True allows charge, else discharge
        delta_bc = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        if self.strict_pos_coeffs:
            # auxiliary binary variable for imposing positive allocation coefficients
            delta_coeff = dict_none_lists(self.time_intervals, self.set_meters)
        if self.total_share_coeffs:
            # auxiliary binary variable for signaling if the REC has a surplus or a deficit
            delta_rec_balance = none_lists(self.time_intervals)
            # auxiliary binary variable for signaling if a meter has a surplus or a deficit
            delta_meter_balance = dict_none_lists(self.time_intervals, self.set_meters)

        # Define the decision variables as puLP objets
        if self.total_share_coeffs:
            for t in self.time_series:
                increment = f't{t:03d}'
                delta_rec_balance[t] = LpVariable('delta_rec_balance_' + increment, cat=LpBinary)

        t_n_series = itertools.product(self.set_meters, self.time_series)  # iterates over each Meter and each time step
        for n, t in t_n_series:
            increment = f'{n}_t{t:03d}'
            e_sup_retail[n][t] = LpVariable('e_sup_retail_' + increment, lowBound=0)
            e_sur_retail[n][t] = LpVariable('e_sur_retail_' + increment, lowBound=0)
            e_sup_market[n][t] = LpVariable('e_sup_market_' + increment, lowBound=0)
            e_sur_market[n][t] = LpVariable('e_sur_market_' + increment, lowBound=0)
            delta_sup[n][t] = LpVariable('delta_sup_' + increment, cat=LpBinary)
            e_cmet[n][t] = LpVariable('e_cmet_' + increment)
            e_consumed[n][t] = LpVariable('e_consumed_' + increment, lowBound=0)
            e_alc[n][t] = LpVariable('e_alc_' + increment, lowBound=0)
            delta_slc[n][t] = LpVariable('delta_slc_' + increment, cat=LpBinary)
            delta_cmet[n][t] = LpVariable('delta_cmet_' + increment, cat=LpBinary)
            delta_alc[n][t] = LpVariable('delta_alc_' + increment, cat=LpBinary)
            p_extra[n][t] = LpVariable('p_extra_' + increment, lowBound=0)
            if self.strict_pos_coeffs:
                delta_coeff[n][t] = LpVariable('delta_coeff_' + increment, cat=LpBinary)
            if self.total_share_coeffs:
                delta_meter_balance[n][t] = LpVariable('delta_meter_balance_' + increment, cat=LpBinary)
            for b in self.sets_btm_storage[n]:
                increment = f'{n}_{b}_t{t:03d}'
                e_bat[n][b][t] = LpVariable('e_bat_' + increment, lowBound=0)
                soc_bat[n][b][t] = LpVariable('soc_bat_' + increment, lowBound=0)
                e_bc[n][b][t] = LpVariable('e_bc_' + increment, lowBound=0)
                e_bd[n][b][t] = LpVariable('e_bd_' + increment, lowBound=0)
                delta_bc[n][b][t] = LpVariable('delta_bc_' + increment, cat=LpBinary)
            for m in self.sets_other_meters[n]:
                increment = f'{n}_{m}_t{t:03d}'
                e_pur[n][m][t] = LpVariable('e_pur_' + increment, lowBound=0)
                e_sale[n][m][t] = LpVariable('e_sale_' + increment, lowBound=0)
                e_slc[n][m][t] = LpVariable('e_slc_' + increment, lowBound=0)

        # Eq. 10: Objective Function
        objective = lpSum(
            lpSum(
                e_sup_retail[n][t] * self._l_buy[n][t] - e_sur_retail[n][t] * self._l_sell[n][t]
                + e_sup_market[n][t] * self._l_market_buy[t] - e_sur_market[n][t] * self._l_market_sell[t]
                + lpSum(e_slc[n][m][t] * self._l_grid[n][m][t] for m in self.sets_other_meters[n])
                + p_extra[n][t] * self._l_extra
                + lpSum(self._deg_cost[n][b] * e_bd[n][b][t] for b in self.sets_btm_storage[n])
                for n in self.set_meters
            )
            for t in self.time_series
        )

        self.milp += objective, 'Objective Function'

        # Eq. 11-34: Constraints
        if self.total_share_coeffs:
            for t in self.time_series:
                increment = f'{t:03d}'
                # Eq. 32
                self.milp += \
                    lpSum(e_cmet[n][t] for n in self.set_meters) >= - self._big_m * delta_rec_balance[t], \
                    'Check_REC_surplus_' + increment

                # Eq. 33
                self.milp += \
                    lpSum(e_cmet[n][t] for n in self.set_meters) <= self._big_m * (1 - delta_rec_balance[t]), \
                    'Check_REC_deficit_' + increment

        for n, t in itertools.product(self.set_meters, self.time_series):
            for m in self.sets_other_meters[n]:
                increment = f'{n}_{m}_t{t:03d}'
                # Eq. 11
                self.milp += \
                    e_pur[n][m][t] == e_sale[m][n][t], \
                    'Market_equilibrium_' + increment

                if all([lg[t] >= 0 for _, lg in self._l_grid[n].items()]):
                    # Eq. 35
                    self.milp += \
                        e_slc[n][m][t] >= e_pur[n][m][t] - e_sale[n][m][t] - self._big_m * delta_slc[n][t], \
                        'Self_consumed_is_allocated_' + increment

                    # Eq. 36
                    self.milp += \
                        e_slc[n][m][t] <= e_pur[n][m][t] - e_sale[n][m][t] + self._big_m * (1 - delta_slc[n][t]), \
                        'Self_consumed_is_consumed_' + increment

            increment = f'{n}_t{t:03d}'
            # Eq. 12
            self.milp += \
                e_cmet[n][t] == \
                e_sup_retail[n][t] + e_sup_market[n][t] - e_sur_retail[n][t] - e_sur_market[n][t] \
                + lpSum(e_pur[n][m][t] - e_sale[n][m][t] for m in self.sets_other_meters[n]), \
                'Equilibrium_' + increment

            # Eq. 13
            self.milp += \
                e_cmet[n][t] == self._e_c[n][t] - self._e_g[n][t] \
                + lpSum(e_bc[n][b][t] - e_bd[n][b][t] for b in self.sets_btm_storage[n]), \
                'C_met_' + increment

            # Eq. 14
            self.milp += \
                - p_extra[n][t] - self._p_meter_max[n] <= e_cmet[n][t] * 1 / self._delta_t, \
                'P_flow_low_limit_' + increment

            self.milp += \
                e_cmet[n][t] * 1 / self._delta_t <= p_extra[n][t] + self._p_meter_max[n], \
                'P_flow_high_limit_' + increment

            # Eq. 15
            self.milp += \
                e_sup_retail[n][t] + e_sup_market[n][t] <= self._big_m * delta_sup[n][t], \
                'Supply_ON_' + increment

            self.milp += \
                e_sur_retail[n][t] + e_sur_market[n][t] <= self._big_m * (1 - delta_sup[n][t]), \
                'Supply_OFF_' + increment

            if all([lg[t] >= 0 for _, lg in self._l_grid[n].items()]):
                # Eq. 20
                self.milp += \
                    e_consumed[n][t] >= e_cmet[n][t], \
                    'Consumption_' + increment

                # Eq. 21
                self.milp += \
                    e_alc[n][t] >= lpSum(e_pur[n][m][t] - e_sale[n][m][t] for m in self.sets_other_meters[n]), \
                    'Allocated_energy_' + increment

                # Eq. 22
                self.milp += \
                    lpSum(e_slc[n][m][t] for m in self.sets_other_meters[n]) >= \
                    e_consumed[n][t] - self._big_m * (1 - delta_slc[n][t]), \
                    'Self_consumption_1_' + increment

                # Eq. 23
                self.milp += \
                    lpSum(e_slc[n][m][t] for m in self.sets_other_meters[n]) >= \
                    e_alc[n][t] - self._big_m * delta_slc[n][t], \
                    'Self_consumption_2_' + increment

                # Eq. aux
                self.milp += \
                    delta_cmet[n][t] == 0, \
                    'Consumption_bin_' + increment

                # Eq. aux
                self.milp += \
                    delta_alc[n][t] == 0, \
                    'Allocated_energy_bin_' + increment

            else:
                # Eq. 24
                self.milp += \
                    e_consumed[n][t] <= e_cmet[n][t] + self._big_m * delta_cmet[n][t], \
                    'Consumption_1_' + increment

                # Eq. 25
                self.milp += \
                    e_consumed[n][t] <= self._big_m * (1 - delta_cmet[n][t]), \
                    'Consumption_2_' + increment

                # Eq. 26
                self.milp += \
                    e_alc[n][t] <= lpSum(e_pur[n][m][t] - e_sale[n][m][t] for m in self.sets_other_meters[n]) \
                    + self._big_m * delta_alc[n][t], \
                    'Allocated_energy_1_' + increment

                # Eq. 27
                self.milp += \
                    e_alc[n][t] <= self._big_m * (1 - delta_alc[n][t]), \
                    'Allocated_energy_2_' + increment

                # Eq. 28
                self.milp += \
                    lpSum(e_slc[n][m][t] for m in self.sets_other_meters[n]) <= e_consumed[n][t], \
                    'Self_consumption_1_' + increment

                # Eq. 29
                self.milp += \
                    lpSum(e_slc[n][m][t] for m in self.sets_other_meters[n]) <= e_alc[n][t], \
                    'Self_consumption_2_' + increment

                # Eq. aux
                self.milp += \
                    delta_slc[n][t] == 0, \
                    'Self_consumed_energy_bin_' + increment

            if self.strict_pos_coeffs:
                # Eq. 30
                self.milp += \
                    lpSum(e_sale[n][m][t] - e_pur[n][m][t] for m in self.sets_other_meters[n]) <= \
                    -e_cmet[n][t] + self._big_m * delta_coeff[n][t], \
                    'Positive_coefficients_1_' + increment

                # Eq. 31
                self.milp += \
                    lpSum(e_sale[n][m][t] - e_pur[n][m][t] for m in self.sets_other_meters[n]) <= \
                    self._big_m * (1 - delta_coeff[n][t]), \
                    'Positive_coefficients_2_' + increment

            if self.total_share_coeffs:
                # Eq. 34
                self.milp += \
                    e_cmet[n][t] >= - self._big_m * delta_meter_balance[n][t], \
                    'Check_meter_surplus_' + increment

                # Eq. 35
                self.milp += \
                    e_cmet[n][t] <= self._big_m * (1 - delta_meter_balance[n][t]), \
                    'Check_meter_deficit_' + increment

                # Eq. 36
                self.milp += \
                    lpSum(e_sale[n][m][t] for m in self.sets_other_meters[n]) >= \
                    - e_cmet[n][t] - self._big_m * (1 - delta_meter_balance[n][t] + delta_rec_balance[t]), \
                    'Share_all_surplus_low_' + increment

                # Eq. 37
                self.milp += \
                    lpSum(e_sale[n][m][t] for m in self.sets_other_meters[n]) <= \
                    - e_cmet[n][t] + self._big_m * (1 - delta_meter_balance[n][t] + delta_rec_balance[t]), \
                    'Share_all_surplus_high_' + increment

                # Eq. 38
                self.milp += \
                    lpSum(e_pur[n][m][t] for m in self.sets_other_meters[n]) >= \
                    e_cmet[n][t] - self._big_m * (1 - delta_rec_balance[t] + delta_meter_balance[n][t]), \
                    'Buy_all_deficit_low_' + increment

                # Eq. 39
                self.milp += \
                    lpSum(e_pur[n][m][t] for m in self.sets_other_meters[n]) <= \
                    e_cmet[n][t] + self._big_m * (1 - delta_rec_balance[t] + delta_meter_balance[n][t]), \
                    'Buy_all_deficit_high_' + increment

            for b in self.sets_btm_storage[n]:
                increment = f'{n}_{b}_t{t:03d}'

                if self._e_bn[n][b] > 0:
                    # Eq. 16
                    energy_update = e_bc[n][b][t] * self._eff_bc[n][b] \
                                    - e_bd[n][b][t] * 1 / self._eff_bd[n][b]
                    if t == 0:
                        self.milp += \
                            e_bat[n][b][t] == self._init_e_bat[n][b] + energy_update, \
                            'SOC_update_' + increment
                    else:
                        self.milp += \
                            e_bat[n][b][t] == e_bat[n][b][t - 1] + energy_update, \
                            'SOC_update_' + increment

                    # Eq. 17
                    self.milp += \
                        soc_bat[n][b][t] == e_bat[n][b][t] * 100 / self._e_bn[n][b], \
                        'Energy_to_SOC_' + increment

                    self.milp += \
                        soc_bat[n][b][t] >= self._soc_min[n][b], \
                        'Minimum_SOC_' + increment

                    self.milp += \
                        soc_bat[n][b][t] <= self._soc_max[n][b], \
                        'Maximum_SOC_' + increment

                    # Eq. 18
                    self.milp += \
                        e_bc[n][b][t] * 1 / self._delta_t <= self._p_max[n][b] * delta_bc[n][b][t], \
                        'Charge_rate_limit_' + increment

                    self.milp += \
                        e_bd[n][b][t] * 1 / self._delta_t <= self._p_max[n][b] * (1 - delta_bc[n][b][t]), \
                        'Discharge_rate_limit' + increment

        # Calculate indexes
        self.discount_function()

        for n in self.set_meters:
            increment = f'{n}'

            # Eq. 19
            self.milp += lpSum(
                e_sup_retail[n][t] * self._l_buy[n][t] - e_sur_retail[n][t] * self._l_sell[n][t]
                + e_sup_market[n][t] * self._l_market_buy[t] - e_sur_market[n][t] * self._l_market_sell[t]
                + lpSum(
                    e_slc[n][m][t] * self._l_grid[n][m][t] for m in self.sets_other_meters[n])
                + p_extra[n][t] * self._l_extra
                + lpSum(self._deg_cost[n][b] * e_bd[n][b][t] for b in self.sets_btm_storage[n])
                + (lpSum(e_pur[n][m][t] - e_sale[n][m][t] for m in self.sets_other_meters[n])) * self._l_lem[t]
                for t in self.time_series) <= round_up(self._c_ind[n]), 'Stage_1_cost_' + increment

        # Write MILP to .lp file
        dir_name = os.path.abspath(os.path.join(__file__, '..'))
        lp_file = os.path.join(dir_name, f'Stage2Bilateral.lp')
        self.milp.writeLP(lp_file)

        # Set the solver to be called
        if self.solver == 'CBC':
            self.milp.setSolver(pulp.PULP_CBC_CMD(msg=False, timeLimit=self.timeout, gapRel=self.mipgap))

        elif self.solver == 'GUROBI':
            # self.milp.setSolver(pulp.GUROBI_CMD(msg=True, timeLimit=self.timeout, mip=self.mipgap))
            self.milp.setSolver(GUROBI_CMD(msg=False))

        else:
            raise ValueError

        logger.debug('-- defining the collective (bilateral) MILP problem... DONE!')

        return

    # Calculate vulnerability and non-vulnerability indexes
    def discount_function(self):
        ref1 = 0.10
        ref2 = 7000
        ref3 = 150 * 12
        storage = {}
        res = {}

        for n in self.set_meters:
            if self._meters_data[n]['btm_storage'] is not None:
                storage[n] = 1
            else:
                storage[n] = 0

            if sum(self._meters_data[n]['e_g']) != 0:
                res[n] = 1
            else:
                res[n] = 0

        for i in self.set_meters:
            self.epi[i] = int(
                self.energy_exp[i] / self.annual_income[i] > ref1 and (self.annual_income[i] / self.n_ind[i] -
                                                                       ref3 * self.n_ind_no_inc[i]) < ref2)

            if self.epi[i] == 0:
                if (1 - self.energy_exp[i] / self.annual_income[i]) > 0.96:
                    self.norm_1[i] = (1 - self.energy_exp[i] / self.annual_income[i]) * 2
                else:
                    self.norm_1[i] = 1 - self.energy_exp[i] / self.annual_income[i] * 3
                self.norm_2[i] = self.annual_income[i] / self.n_ind[i] * (1 + 0.15 * storage[i] + 0.35 * res[i])
                if self.n_ind_no_inc[i] != 0:
                    if self.n_ind[i] != 1:
                        self.norm_3[i] = self.n_ind[i] / self.n_ind_no_inc[i]
                    else:
                        self.norm_3[i] = self.n_ind[i] / self.n_ind_no_inc[i] / 2
                else:
                    self.norm_3[i] = 0
            else:
                if self.energy_exp[i] / self.annual_income[i] < 0.12:
                    self.norm_1[i] = self.energy_exp[i] / self.annual_income[i]
                elif 0.12 <= self.energy_exp[i] / self.annual_income[i] < 0.14:
                    self.norm_1[i] = self.energy_exp[i] / self.annual_income[i] * 2
                else:
                    self.norm_1[i] = self.energy_exp[i] / self.annual_income[i] * 3

                self.norm_2[i] = ref2 - self.annual_income[i] / self.n_ind[i]

                if self.n_ind_no_inc[i] != 0 and self.n_ind[i] == 1:
                    self.norm_3[i] = self.n_ind_no_inc[i] / self.n_ind[i] * 2
                else:
                    self.norm_3[i] = self.n_ind_no_inc[i] / self.n_ind[i]

        sum1 = 0
        sum2 = 0
        sum3 = 0
        sum1_nv = 0
        sum2_nv = 0
        sum3_nv = 0
        for n in self.set_meters:
            if self.epi[n] == 1:
                sum1 += self.norm_1[n]
                sum2 += self.norm_2[n]
                sum3 += self.norm_3[n]
            else:
                sum1_nv += self.norm_1[n]
                sum2_nv += self.norm_2[n]
                sum3_nv += self.norm_3[n]

        for n in self.set_meters:
            if self.epi[n] == 1:
                if sum3 != 0:
                    self.epi_cont[n] = 0.20 * self.norm_1[n] / sum1 + 0.60 * self.norm_2[n] / sum2 + \
                                   0.20 * self.norm_3[n] / sum3
                    self.nvi[n] = 0
                else:
                    self.epi_cont[n] = 0.20 * self.norm_1[n] / sum1 + 0.60 * self.norm_2[n] / sum2
                    self.nvi[n] = 0
            else:
                self.nvi[n] = 0.15 * self.norm_1[n] / sum1_nv + 0.70 * self.norm_2[n] / sum2_nv + \
                              0.15 * self.norm_3[n] / sum3_nv
                self.epi_cont[n] = 0

        print('nvi:', self.nvi)
        print('epi:', self.epi_cont)

        return self.epi_cont

    def solve_milp(self):
        """
        Function that heads the definition and solution of the second stage MILP.
        """

        # Define the MILP
        self.__define_milp()

        # Solve the MILP
        logger.debug('-- solving the collective (bilateral) MILP problem...')

        try:
            self.milp.solve()
            status = LpStatus[self.milp.status]
            opt_value = value(self.milp.objective)

        except Exception as e:
            logger.warning(f'Solver raised an error: \'{e}\'. Considering problem as "Infeasible".')
            status = 'Infeasible'
            opt_value = None

        self.status = status
        self.obj_value = opt_value

        logger.debug('-- solving the collective (bilateral) MILP problem... DONE!')

        return

    def generate_outputs(self) -> OutputsS2BilateralDict:
        """
        Function for generating the outputs of optimization, namely the battery's set points.
        :return: outputs dictionary with MILP variables' and other computed values
        """
        logger.debug('-- generating outputs from the collective (bilateral) MILP problem...')

        outputs = {}

        # -- Verification added to avoid raising error whenever encountering a puLP solver error with CBC
        if self.obj_value is None:
            return outputs

        outputs['obj_value'] = self.obj_value
        outputs['milp_status'] = self.status

        outputs['e_sup_retail'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_sur_retail'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_sup_market'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_sur_market'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['delta_sup'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_pur_bilateral'] = \
            {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        outputs['e_sale_bilateral'] = \
            {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        outputs['e_cmet'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_slc_bilateral'] = \
            {n: dict_none_lists(self.time_intervals, self.sets_other_meters[n]) for n in self.set_meters}
        outputs['e_consumed'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_alc'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['delta_slc'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['delta_cmet'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['delta_alc'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['p_extra'] = dict_none_lists(self.time_intervals, self.set_meters)
        outputs['e_bat'] = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        outputs['soc_bat'] = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in
                              self.set_meters}
        outputs['e_bc'] = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        outputs['e_bd'] = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in self.set_meters}
        outputs['delta_bc'] = {n: dict_none_lists(self.time_intervals, self.sets_btm_storage[n]) for n in
                               self.set_meters}
        if self.strict_pos_coeffs:
            outputs['delta_coeff'] = dict_none_lists(self.time_intervals, self.set_meters)
        if self.total_share_coeffs:
            outputs['delta_rec_balance'] = none_lists(self.time_intervals)
            outputs['delta_meter_balance'] = dict_none_lists(self.time_intervals, self.set_meters)

        # Required when vars include "-" since puLP converts it to "_"
        btm_storage_ids = [bid for bids in [v for _, v in self.sets_btm_storage.items()] for bid in bids]
        matchd = {key: key.replace('-', '_') for key in self.set_meters}
        b_matchd = {key: key.replace('-', '_') for key in btm_storage_ids}

        # Auxiliary lambda functions to extract the neter and batteries' designations from the variables
        original_n_name = \
            lambda v_name: [ori_n for ori_n in self.set_meters if matchd[ori_n] + '_' in v_name][0]
        original_b_name = \
            lambda v_name: [ori_b for ori_b in btm_storage_ids if b_matchd[ori_b] + '_' in v_name][0]
        alt_original_n_name = \
            lambda v_name, v_group: \
                [ori_n for ori_n in self.set_meters if v_group + matchd[ori_n] in v.name][0]
        original_m_name = \
            lambda v_name, v_group: \
                [ori_m for ori_m in self.set_meters if (matchd[ori_m] in v_name) and
                 (matchd[ori_m] != alt_original_n_name(v_name, v_group))][0]

        # Associate the values of the variables with the respective outputs' structure
        for v in self.milp.variables():
            if re.search('dummy', v.name):
                continue

            # Returns the variable name termination "t000"
            step_nr = int(v.name[-3:])

            if re.search(f'e_sup_retail_', v.name):
                n = original_n_name(v.name)
                outputs['e_sup_retail'][n][step_nr] = v.varValue
            elif re.search(f'e_sur_retail_', v.name):
                n = original_n_name(v.name)
                outputs['e_sur_retail'][n][step_nr] = v.varValue
            elif re.search(f'e_sup_market_', v.name):
                n = original_n_name(v.name)
                outputs['e_sup_market'][n][step_nr] = v.varValue
            elif re.search(f'e_sur_market_', v.name):
                n = original_n_name(v.name)
                outputs['e_sur_market'][n][step_nr] = v.varValue
            elif re.search(f'delta_sup_', v.name):
                n = original_n_name(v.name)
                outputs['delta_sup'][n][step_nr] = v.varValue
            elif re.search(f'e_pur_', v.name):
                n = alt_original_n_name(v.name, 'e_pur_')
                m = original_m_name(v.name, 'e_pur_')
                outputs['e_pur_bilateral'][n][m][step_nr] = v.varValue
            elif re.search(f'e_sale_', v.name):
                n = alt_original_n_name(v.name, 'e_sale_')
                m = original_m_name(v.name, 'e_sale_')
                outputs['e_sale_bilateral'][n][m][step_nr] = v.varValue
            elif re.search(f'e_cmet_', v.name):
                n = original_n_name(v.name)
                outputs['e_cmet'][n][step_nr] = v.varValue
            elif re.search(f'e_slc_', v.name):
                n = alt_original_n_name(v.name, 'e_slc_')
                m = original_m_name(v.name, 'e_slc_')
                outputs['e_slc_bilateral'][n][m][step_nr] = v.varValue
            elif re.search(f'e_consumed_', v.name):
                n = original_n_name(v.name)
                outputs['e_consumed'][n][step_nr] = v.varValue
            elif re.search(f'e_alc_', v.name):
                n = original_n_name(v.name)
                outputs['e_alc'][n][step_nr] = v.varValue
            elif re.search(f'delta_slc_', v.name):
                n = original_n_name(v.name)
                outputs['delta_slc'][n][step_nr] = v.varValue
            elif re.search(f'delta_cmet_', v.name):
                n = original_n_name(v.name)
                outputs['delta_cmet'][n][step_nr] = v.varValue
            elif re.search(f'delta_alc_', v.name):
                n = original_n_name(v.name)
                outputs['delta_alc'][n][step_nr] = v.varValue
            elif re.search(f'delta_coeff_', v.name):
                n = original_n_name(v.name)
                outputs['delta_coeff'][n][step_nr] = v.varValue
            elif re.search(f'delta_meter_balance_', v.name):
                n = original_n_name(v.name)
                outputs['delta_meter_balance'][n][step_nr] = v.varValue
            elif re.search(f'p_extra_', v.name):
                n = original_n_name(v.name)
                outputs['p_extra'][n][step_nr] = v.varValue
            elif re.search(f'e_bat_', v.name):
                n = original_n_name(v.name)
                b = original_b_name(v.name)
                outputs['e_bat'][n][b][step_nr] = v.varValue
            elif re.search(f'soc_bat_', v.name):
                n = original_n_name(v.name)
                b = original_b_name(v.name)
                outputs['soc_bat'][n][b][step_nr] = v.varValue
            elif re.search(f'e_bc_', v.name):
                n = original_n_name(v.name)
                b = original_b_name(v.name)
                outputs['e_bc'][n][b][step_nr] = v.varValue
            elif re.search(f'e_bd_', v.name):
                n = original_n_name(v.name)
                b = original_b_name(v.name)
                outputs['e_bd'][n][b][step_nr] = v.varValue
            elif re.search(f'delta_bc_', v.name):
                n = original_n_name(v.name)
                b = original_b_name(v.name)
                outputs['delta_bc'][n][b][step_nr] = v.varValue

        # Include other individual cost metrics
        outputs['c_ind2bilateral'] = {n: None for n in self.set_meters}
        outputs['c_ind2bilateral_without_deg'] = {n: None for n in self.set_meters}
        outputs['c_ind2bilateral_without_deg_and_p_extra'] = {n: None for n in self.set_meters}
        outputs['c_ind2bilateral_without_p_extra'] = {n: None for n in self.set_meters}
        outputs['deg_cost2bilateral'] = {n: None for n in self.set_meters}
        outputs['p_extra_cost2bilateral'] = {n: None for n in self.set_meters}

        # Calculate individual costs' penalties for non-vulnerable consumers
        total_epur = 0
        nind = 0
        ref = 0.04
        for n in self.set_meters:

            if self.epi[n] == 1:
                nind += 1

            epur = []
            for key in outputs['e_pur_bilateral'][n]:
                epur.append(outputs['e_pur_bilateral'][n][key])
            epur_sum = [sum(elts) for elts in zip(*epur)]

            result1 = sum(epur_sum[i] * self._l_lem[i] for i in range(len(epur_sum)))
            total_epur += result1

        if ref * nind > 1 / 3:
            penalty = total_epur * 1/3
        else:
            penalty = total_epur * ref * nind
        print('penalty:', penalty)

        # Calculate individual penalties
        penalty_per_consumer = {}
        ni = 0
        for n in self.set_meters:
            if self.epi[n] == 1:
                ni += 1
        if self.method == 1:
            for n in self.set_meters:
                if ni != 0:
                    penalty_per_consumer[n] = penalty / (len(self.set_meters) - ni)
                else:
                    penalty_per_consumer[n] = 0
        elif self.method == 2:
            for n in self.set_meters:
                penalty_per_consumer[n] = penalty * self.nvi[n]

        print('penalty/consumer:', penalty_per_consumer)

        # Calculate the individual costs found on stage 2
        rematchd = {v: k for k, v in matchd.items()}
        constraints = [self.milp.constraints[c] for c in self.milp.constraints if c.startswith('Stage_1_cost_')]
        for constraint in constraints:
            # Calculate the cost that came from overstepping the maximum Meter power limit
            n = rematchd[constraint.name.split('Stage_1_cost_')[-1]]
            p_extra = sum(outputs['p_extra'][n])
            p_extra_cost = p_extra * self._l_extra
            outputs['p_extra_cost2bilateral'][n] = p_extra_cost

            # Calculate the cost of degradation
            deg_cost = 0
            for b, t in itertools.product(self.sets_btm_storage[n], self.time_series):
                deg_cost += self._deg_cost[n][b] * outputs['e_bd'][n][b][t]
            outputs['deg_cost2bilateral'][n] = deg_cost

            # Retrieve the cost with energy of each Meter obtained in Stage 2
            constraint_sum = 0
            for var, coefficient in constraint.items():
                constraint_sum += var.varValue * coefficient
            constraint_sum += constraint.constant + self._c_ind[n]

            if self.epi[n] == 1:
                outputs['c_ind2bilateral'][n] = constraint_sum - penalty * self.epi_cont[n]
            else:
                outputs['c_ind2bilateral'][n] = constraint_sum + penalty_per_consumer[n]

            # Calculate additional terms that do not consider the cost of degradation and/or extra power at Meter:
            outputs['c_ind2bilateral_without_deg'][n] = (
                    outputs['c_ind2bilateral'][n] - deg_cost)
            outputs['c_ind2bilateral_without_p_extra'][n] = (
                    outputs['c_ind2bilateral'][n] - p_extra_cost)
            outputs['c_ind2bilateral_without_deg_and_p_extra'][n] = (
                    outputs['c_ind2bilateral'][n] - deg_cost - p_extra_cost)

        # Adjust penalty distribution, making sure that the individual costs do not surpass the values from stage 1
        print('c_ind2 b4 adjust:', outputs['c_ind2bilateral'])
        excluded = []
        vuln = []
        pen = 1
        while pen != 0:
            pen = 0
            for x in self.set_meters:
                if self.epi[x] == 1:
                    if x not in excluded:
                        vuln.append(x)
                if outputs['c_ind2bilateral'][x] > 1.0001 * self._c_ind[x]:
                    if x not in excluded:
                        pen += (outputs['c_ind2bilateral'][x] - self._c_ind[x])
                        excluded.append(x)

            sum1_nv = 0
            sum2_nv = 0
            sum3_nv = 0
            for x in self.set_meters:
                if x not in excluded:
                    sum1_nv += self.norm_1[x]
                    sum2_nv += self.norm_2[x]
                    sum3_nv += self.norm_3[x]

            for x in self.set_meters:
                if x not in (excluded and vuln):
                    if self.method == 1:
                        if len(self.set_meters) != (len(excluded) + len(vuln)):
                            outputs['c_ind2bilateral'][x] += pen / (len(self.set_meters) - len(excluded) - len(vuln))
                    elif self.method == 2:
                        if sum1_nv != 0:
                            self.nvi[x] = 0.20 * self.norm_1[x] / sum1_nv + 0.60 * self.norm_2[x] / sum2_nv + \
                                          0.20 * self.norm_3[x] / sum3_nv
                            outputs['c_ind2bilateral'][x] += pen * self.nvi[x]

            if (len(excluded) + len(vuln)) == len(self.set_meters) or excluded == []:
                break

        print('excluded:', excluded)

        for x in excluded:
            outputs['c_ind2bilateral'][x] = self._c_ind[x]

        logger.debug('-- generating outputs from the collective (bilateral) MILP problem... DONE!')

        return outputs


if __name__ == '__main__':
    from rec_op_lem_prices.optimization.structures.I_O_stage_2_bilateral_milp import (
        INPUTS_S2_BILATERAL,
        OUTPUTS_S2_BILATERAL
    )

    # Assert the creation of a correct class
    milp = StageTwoMILPBilateral({'delta_t': 1.0,
                                  'horizon': 5.0,
                                  'l_extra': 10,
                                  'l_grid': {
                                      'Meter#1': {
                                          'Meter#2': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#3': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#4': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#5': [0.01, 0.01, 0.01, 0.01, 0.01]
                                      },
                                      'Meter#2': {
                                          'Meter#1': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#3': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#4': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#5': [0.01, 0.01, 0.01, 0.01, 0.01]
                                      },
                                      'Meter#3': {
                                          'Meter#1': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#2': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#4': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#5': [0.01, 0.01, 0.01, 0.01, 0.01]
                                      },
                                      'Meter#4': {
                                          'Meter#1': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#2': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#3': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#5': [0.01, 0.01, 0.01, 0.01, 0.01]
                                      },
                                      'Meter#5': {
                                          'Meter#1': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#2': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#3': [0.01, 0.01, 0.01, 0.01, 0.01],
                                          'Meter#4': [0.01, 0.01, 0.01, 0.01, 0.01]
                                      }
                                  },
                                  'l_lem': [1.5, 1.5, 1.5, 1.5, 1.5],
                                  'l_market_buy': [3.0, 3.0, 3.0, 3.0, 3.0],
                                  'l_market_sell': [0.1, 0.1, 0.1, 0.1, 0.1],
                                  'meters': {
                                      'Meter#1': {
                                          'btm_storage': {
                                              'Storage#1': {
                                                  'degradation_cost': 0.01,
                                                  'e_bn': 1.0,
                                                  'eff_bc': 1.0,
                                                  'eff_bd': 1.0,
                                                  'init_e': 0.0,
                                                  'p_max': 1.0,
                                                  'soc_max': 100.0,
                                                  'soc_min': 0.0}
                                          },
                                          'e_c': [0.0, 0.5, 0.1, 0.3, 0.2],
                                          'e_g': [0.0, 0.2, 0.8, 0.3, 0.0],
                                          'l_buy': [2.0, 2.0, 2.0, 2.0, 2.0],
                                          'l_sell': [0.0, 0.0, 0.2, 0.5, 0.9],
                                          'max_p': 5.0
                                      },
                                      'Meter#2': {
                                          'btm_storage': {
                                              'Storage#2': {
                                                  'degradation_cost': 0.01,
                                                  'e_bn': 1.0,
                                                  'eff_bc': 1.0,
                                                  'eff_bd': 1.0,
                                                  'init_e': 0.0,
                                                  'p_max': 1.0,
                                                  'soc_max': 100.0,
                                                  'soc_min': 0.0}
                                          },
                                          'e_c': [0.1, 0.5, 0.1, 0.3, 0.0],
                                          'e_g': [0.0, 0.4, 0.7, 0.2, 0.0],
                                          'l_buy': [2.2, 2.0, 1.8, 2.1, 2.3],
                                          'l_sell': [0.1, 0.0, 0.0, 0.3, 0.7],
                                          'max_p': 5.0
                                      },
                                      'Meter#3': {
                                          'btm_storage': None,
                                          'e_c': [0.0, 0.2, 0.4, 0.0, 0.1],
                                          'e_g': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'l_buy': [2.1, 2.1, 2.1, 2.1, 2.1],
                                          'l_sell': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'max_p': 5.0
                                      },
                                      'Meter#4': {
                                          'btm_storage': None,
                                          'e_c': [0.0, 0.4, 0.5, 0.2, 0.0],
                                          'e_g': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'l_buy': [2.2, 2.2, 2.2, 2.2, 2.2],
                                          'l_sell': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'max_p': 5.0
                                      },
                                      'Meter#5': {
                                          'btm_storage': None,
                                          'e_c': [0.1, 0.0, 0.6, 0.3, 0.1],
                                          'e_g': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'l_buy': [2.0, 2.0, 2.0, 2.0, 2.0],
                                          'l_sell': [0.0, 0.0, 0.0, 0.0, 0.0],
                                          'max_p': 5.0
                                      }
                                  },
                                  'second_stage': False,
                                  # these inputs are used for testing the second stage of the two-stage approach
                                  'strict_pos_coeffs': True,
                                  'total_share_coeffs': False
                                  })
    assert isinstance(milp, StageTwoMILPBilateral)

    # Assert the MILP is optimally solved
    milp.solve_milp()
    assert milp.status == 'Optimal'

    # Assert the correct outputs
    results = milp.generate_outputs()
    print("obj_value:", results['obj_value'])
    print("c_ind2bilateral:", results['c_ind2bilateral'])
    print("e_sup_retail:", results['e_sup_retail'])
    print("e_sup_market:", results['e_sup_market'])
    print("e_sur_retail:", results['e_sur_retail'])
    print("e_sur_market:", results['e_sur_market'])
    print("e_pur_bilateral:", results['e_pur_bilateral'])
    print("e_sale_bilateral:", results['e_sale_bilateral'])
    print("e_bd:", results['e_bd'])
    print("e_slc_bilateral:", results['e_slc_bilateral'])
    round_cost = lambda x: {meter_id: round(cost, 3) for meter_id, cost in x.items()}
    results['obj_value'] = round(results['obj_value'], 3)
    results['c_ind2bilateral'] = round_cost(results['c_ind2bilateral'])
    results['c_ind2bilateral_without_deg'] = round_cost(results['c_ind2bilateral_without_deg'])
    results['c_ind2bilateral_without_deg_and_p_extra'] = round_cost(results['c_ind2bilateral_without_deg_and_p_extra'])
    results['c_ind2bilateral_without_p_extra'] = round_cost(results['c_ind2bilateral_without_p_extra'])
