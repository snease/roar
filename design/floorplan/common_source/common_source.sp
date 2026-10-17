* Synthetic geometry example: W per finger, dimensions in metres.
* Generic RLOAD is an export example; SKY130 resistor generation needs a qualified cell.
.subckt common_source VIN VOUT VDD VSS
M1 VOUT VIN VSS VSS sky130_fd_pr__nfet_01v8 W=1.1u L=0.5u nf=2
RLOAD VDD VOUT 10000
.ends common_source
