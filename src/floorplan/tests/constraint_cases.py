"""Dependency-free input cases shared by editor and native ALIGN checks."""

NETLIST = '''.subckt pair d1 d2 g s b
M1 d1 g s b sky130_fd_pr__nfet_01v8 W=1u L=0.15u NF=2
M2 d2 g s b sky130_fd_pr__nfet_01v8 W=1u L=0.15u NF=2
.ends pair
'''
CASES = [
    {'constraint': 'Boundary', 'max_width': 50, 'max_height': 40, 'halo_horizontal': 1.5, 'halo_vertical': 2},
    {'constraint': 'AspectRatio', 'ratio_low': 0.5, 'ratio_high': 2, 'weight': 2},
    {'constraint': 'Spread', 'instances': ['M1', 'M2'], 'direction': 'horizontal', 'distance': 420},
    {'constraint': 'BlockDistance', 'abs_distance': 420},
    {'constraint': 'HorizontalDistance', 'abs_distance': 420},
    {'constraint': 'VerticalDistance', 'abs_distance': 280},
    {'constraint': 'SameTemplate', 'instances': ['M1', 'M2']},
    {'constraint': 'AlignInOrder', 'instances': ['M1', 'M2'], 'line': 'center', 'direction': 'vertical', 'abut': False},
    {'constraint': 'PortLocation', 'ports': ['d1'], 'location': 'TC'},
    {'constraint': 'SymmetricNets', 'net1': 'd1', 'net2': 'd2', 'pins1': ['M1/D', 'd1'], 'pins2': ['M2/D', 'd2'], 'direction': 'V'},
    {'constraint': 'ClockPorts', 'ports': ['g'], 'propagate': False},
    {'constraint': 'DoNotIdentify', 'instances': ['M1']},
    {'constraint': 'ConfigureCompiler', 'merge_series_devices': False, 'merge_parallel_devices': False, 'auto_constraint': False},
    {'constraint': 'NetPriority', 'nets': ['d1'], 'weight': 3},
]
EXPORT_ONLY = [
    {'constraint': 'NetConst', 'nets': ['d1'], 'shield': 's', 'criticality': 3},
    {'constraint': 'MultiConnection', 'nets': ['d1'], 'multiplier': 2},
    {'constraint': 'DoNotRoute', 'nets': ['s']},
    {'constraint': 'DoNotUseLib', 'libraries': ['dp_nmos'], 'propagate': True},
    {'constraint': 'Route', 'min_layer': 'm1', 'max_layer': 'm4', 'customize': [{'nets': ['d1'], 'match': True}]},
]
