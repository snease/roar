* Geometry example only: synthetic sizes, W per finger, dimensions in metres.
* Connectivity from design/cm_ota/gds_25c/cm_ota_align/align_input/current_mirror_ota.sp.
.subckt current_mirror_ota vss vdd vout vinn vinp id
M10 id id vss vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M9 source id vss vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M1 ds1 vinn source vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M2 ds2 vinp source vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M3 ds1 ds1 vdd vdd sky130_fd_pr__pfet_01v8 L=0.15u W=1.05u nf=4
M4 ds2 ds2 vdd vdd sky130_fd_pr__pfet_01v8 L=0.15u W=1.05u nf=4
M5 ds3 ds3 vss vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M6 vout ds3 vss vss sky130_fd_pr__nfet_01v8 L=0.15u W=1.05u nf=4
M7 ds3 ds1 vdd vdd sky130_fd_pr__pfet_01v8 L=0.15u W=1.05u nf=4
M8 vout ds2 vdd vdd sky130_fd_pr__pfet_01v8 L=0.15u W=1.05u nf=4
.ends current_mirror_ota
