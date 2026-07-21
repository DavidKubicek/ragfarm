# Hardware: AceMagic F5X — AMD Ryzen AI 9 HX 370

- CPU: 12 cores / 24 threads — 4× Zen 5 (to 5.1 GHz) + 8× Zen 5c. L2 12 MB, L3 24 MB.
- iGPU: Radeon 890M, 16 RDNA 3.5 CUs (1024 shaders), up to ~2.9 GHz. gfx target gfx1150.
- NPU: XDNA 2, 50 TOPS INT8. Linux platform class "STX". PCI id 1022:17f0.
- Memory: LPDDR5x (up to -8000). 64 GB recommended for the NPU flow.
- IO: PCIe 4.0, USB4.

## TODO for the agent
Record exact BIOS version + AGESA from David Kubicek's BIOS screenshot here once available
(it was provided as an image in the originating chat; transcribe the version
string and any relevant NPU/SMU toggles). Confirm in BIOS that the NPU is enabled
and any "iGPU memory allocation"/UMA settings are set sanely for the 890M.

# AceMagic F5X Hardware Record

Model:         HCAR300_MI3
BIOS Vendor:   American Megatrends International, LLC.
BIOS Version:  1.26
Processor:     AMD Ryzen AI 9 HX 370 w/ Radeon 890M
RAM:           31700 MB (~32GB)
OS:            Ubuntu 24.04.4 LTS
Kernel:        6.17.0-35-generic

## NPU
Device:        RyzenAI-npu4 (XDNA 2 / Strix Point NPU4)
BDF:           0000:c6:00.1
XRT Version:   2.21.75 (branch HEAD, 2026-03-09)
NPU Firmware:  1.0.0.63

## Working kernel parameters for NPU SVA
GRUB_CMDLINE_LINUX_DEFAULT includes: amd_iommu=pgtbl_v2 iommu=on

## Root cause of SVA failure (resolved)
- amd_iommu=force_isolation was blocking SVA bind (-22 EINVAL)
- amd_iommu=pgtbl_v2 required: XDNA 2 needs v2 IOMMU page tables for SVA
- BIOS and hardware were never the problem; both kernel params were wrong
- Without any param: -95 EOPNOTSUPP (IOMMU not initializing)
- With force_isolation: -22 EINVAL (isolation blocked SVA binding)
- With pgtbl_v2 + iommu=on: driver loads cleanly
EOF

## DKMS module — required, in-tree insufficient
In-tree 6.17 amdxdna (0.0.0): no PASID, BO size limit too small, SVA fails
DKMS xrt-amdxdna/2.21.260102.53.release (1.0.0): PASID enabled, works

Install prerequisites:
  sudo apt install dkms linux-headers-$(uname -r)
  sudo bash /opt/xilinx/xrt/share/amdxdna/dkms_driver.sh --install

Confirmed by: PASID address mode enabled in dmesg + quicktest.py: Test Finished


