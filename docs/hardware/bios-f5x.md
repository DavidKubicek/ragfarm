# Hardware: AceMagic F5X — AMD Ryzen AI 9 HX 370

- CPU: 12 cores / 24 threads — 4× Zen 5 (to 5.1 GHz) + 8× Zen 5c. L2 12 MB, L3 24 MB.
- iGPU: Radeon 890M, 16 RDNA 3.5 CUs (1024 shaders), up to ~2.9 GHz. gfx target gfx1150.
- NPU: XDNA 2, 50 TOPS INT8. Linux platform class "STX". PCI id 1022:17f0.
- Memory: LPDDR5x (up to -8000). 64 GB recommended for the NPU flow.
- IO: PCIe 4.0, USB4.

## TODO for the agent
Record exact BIOS version + AGESA from Dave's BIOS screenshot here once available
(it was provided as an image in the originating chat; transcribe the version
string and any relevant NPU/SMU toggles). Confirm in BIOS that the NPU is enabled
and any "iGPU memory allocation"/UMA settings are set sanely for the 890M.
