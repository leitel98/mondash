"""Panel registry. Add a module with a Panel subclass here and it becomes usable in dash.toml and as a script."""
from .cpu import CpuPanel
from .mem import MemPanel
from .net import NetPanel
from .disk import DiskPanel
from .proc import ProcPanel
from .gpu import GpuPanel
from .temp import TempPanel
from .power import PowerPanel
from .sysinfo import SysPanel
from .dl import DlPanel
from .cmd import CmdPanel
from .hw import HwPanel
from .gif import GifPanel
from .jobs import JobsPanel

TYPES = {p.name: p for p in (CpuPanel, MemPanel, NetPanel, DiskPanel, ProcPanel, GpuPanel, TempPanel, PowerPanel,
                             SysPanel, DlPanel, CmdPanel, HwPanel, GifPanel, JobsPanel)}
