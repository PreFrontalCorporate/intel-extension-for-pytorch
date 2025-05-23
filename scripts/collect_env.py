# Referenced from https://github.com/pytorch/pytorch/blob/master/torch/utils/collect_env.py
# Run it with `python collect_env.py`.
import json
import locale
import re
import subprocess
import sys
import os
from collections import namedtuple

try:
    import torch

    TORCH_AVAILABLE = True
except (ImportError, NameError, AttributeError, OSError):
    TORCH_AVAILABLE = False

try:
    import intel_extension_for_pytorch as ipex

    IPEX_AVAILABLE = True
except (ImportError, NameError, AttributeError, OSError):
    IPEX_AVAILABLE = False


# System Environment Information
SystemEnv = namedtuple(
    "SystemEnv",
    [
        "torch_version",
        "torch_cxx11_abi",
        "ipex_version",
        "ipex_gitrev",
        "build_type",
        "gcc_version",
        "clang_version",
        "icx_version",
        "cmake_version",
        "os",
        "libc_version",
        "python_version",
        "python_platform",
        "is_xpu_available",
        "dpcpp_runtime_version",
        "mkl_version",
        "gpu_models_onboard",
        "gpu_models_detected",
        "driver_version",
        "python_env",
        "python_packages",
        "cpu_info",
    ],
)


def run(command):
    """Returns (return-code, stdout, stderr)"""
    my_env = os.environ.copy()
    my_env["LC_ALL"] = "C"
    p = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=my_env, shell=True
    )
    raw_output, raw_err = p.communicate()
    rc = p.returncode
    if get_platform() == "win32":
        enc = "oem"
    else:
        enc = locale.getpreferredencoding()
    output = raw_output.decode(enc)
    err = raw_err.decode(enc)
    return rc, output.strip(), err.strip()


def run_and_read_all(run_lambda, command):
    """Runs command using run_lambda; reads and returns entire output if rc is 0"""
    rc, out, _ = run_lambda(command)
    if rc != 0:
        return None
    return out


def run_and_parse_first_match(run_lambda, command, regex):
    """Runs command using run_lambda, returns the first regex match if it exists"""
    rc, out, _ = run_lambda(command)
    if rc != 0:
        return None
    match = re.search(regex, out)
    if match is None:
        return None
    return match.group(1)


def run_and_return_first_line(run_lambda, command):
    """Runs command using run_lambda and returns first line if output is not empty"""
    rc, out, _ = run_lambda(command)
    if rc != 0:
        return None
    return out.split("\n")[0]


def get_gcc_version(run_lambda):
    return run_and_parse_first_match(run_lambda, "gcc --version", r"gcc (.*)")


def get_clang_version(run_lambda):
    return run_and_parse_first_match(
        run_lambda, "clang --version", r"clang version (.*)"
    )


def get_icx_version(run_lambda):
    return run_and_parse_first_match(
        run_lambda, "icx --version", r"Intel\(R\) oneAPI DPC\+\+\/C\+\+ Compiler (.*)"
    )


def get_cmake_version(run_lambda):
    return run_and_parse_first_match(run_lambda, "cmake --version", r"cmake (.*)")


def get_pkg_version(run_lambda, pkg):
    ret = ""
    index = -1
    if get_platform() == "linux":
        mgr_name = ""
        if mgr_name == "":
            rc, _, _ = run("which dpkg")
            if rc == 0:
                mgr_name = "dpkg"
        if mgr_name == "":
            rc, _, _ = run("which dnf")
            if rc == 0:
                mgr_name = "dnf"
        if mgr_name == "":
            rc, _, _ = run("which yum")
            if rc == 0:
                mgr_name = "yum"
        if mgr_name == "":
            rc, _, _ = run("which zypper")
            if rc == 0:
                mgr_name = "zypper"
        if mgr_name != "":
            cmd = ""
            if mgr_name in ["dnf", "yum", "zypper"]:
                pkg_name = ""
                if pkg == "intel_opencl":
                    pkg_name = "intel-opencl"
                if pkg == "level_zero":
                    pkg_name = "intel-level-zero-gpu"
                if pkg_name != "":
                    if mgr_name in ["dnf", "yum"]:
                        index = 1
                        cmd = f"{mgr_name} list | grep {pkg_name}"
                    if mgr_name in ["zypper"]:
                        index = 2
                        cmd = f"{mgr_name} info {pkg_name} | grep Version"
            if mgr_name == "dpkg":
                index = 2
                pkg_name = ""
                if pkg == "intel_opencl":
                    pkg_name = "intel-opencl-icd"
                if pkg == "level_zero":
                    pkg_name = "intel-level-zero-gpu"
                if pkg_name != "":
                    cmd = f"{mgr_name} -l | grep {pkg_name}"
            if cmd != "":
                ret = run_and_read_all(run_lambda, cmd)
    lst = []
    if ret:
        lst += re.sub(" +", " ", ret).split(" ")
    if len(lst) > index and index != -1:
        ret = lst[index]
    else:
        ret = "N/A"
    return ret


def get_driver_version(run_lambda):
    lst = []
    platform = get_platform()
    if platform == "linux":
        pkgs = ["intel_opencl", "level_zero"]
        for pkg in pkgs:
            lst.append(f"* {pkg}:\t{get_pkg_version(run_lambda, pkg)}")
    if platform == "win32" or platform == "cygwin":
        txt = run_and_read_all(
            run_lambda,
            'powershell.exe "gwmi -Class Win32_PnpSignedDriver | where{$_.DeviceClass -eq \\"DISPLAY\\"\
            -and $_.Manufacturer -match \\"Intel\\"} | Select-Object -Property DeviceName,DriverVersion,DriverDate\
            | ConvertTo-Json"',
        )
        try:
            obj = json.loads(txt)
            if type(obj) is list:
                for o in obj:
                    lst.append(
                        f'* {o["DeviceName"]}: {o["DriverVersion"]} ({o["DriverDate"]})'
                    )
            else:
                lst.append(f'* {obj["DriverVersion"]} ({obj["DriverDate"]})')
        except ValueError as e:
            lst.append(txt)
            lst.append(str(e))
    return "\n".join(lst)


def get_gpu_info_onboard(run_lambda):
    lst = []
    platform = get_platform()
    if platform == "linux":
        txt = run_and_read_all(run_lambda, "xpu-smi discovery -j")
        if txt:
            try:
                obj = json.loads(txt)
                for o in obj["device_list"]:
                    lst.append(f'* {o["device_name"]}')
            except ValueError as e:
                lst.append(txt)
                lst.append(str(e))
        else:
            lst.append("N/A")
    if platform == "win32" or platform == "cygwin":
        txt = run_and_read_all(
            run_lambda,
            'powershell.exe "gwmi -Class Win32_PnpSignedDriver | where{$_.DeviceClass -eq \\"DISPLAY\\"\
            -and $_.Manufacturer -match \\"Intel\\"} | Select-Object -Property DeviceName | ConvertTo-Json"',
        )
        try:
            obj = json.loads(txt)
            if type(obj) is list:
                for o in obj:
                    lst.append(f'* {o["DeviceName"]}')
            else:
                lst.append(f'* {obj["DeviceName"]}')
        except ValueError as e:
            lst.append(txt)
            lst.append(str(e))
    return "\n".join(lst)


def get_gpu_info_detected(run_lambda):
    if TORCH_AVAILABLE and IPEX_AVAILABLE:
        devices = [
            f"* [{i}] {torch.xpu.get_device_properties(i)}"
            for i in range(torch.xpu.device_count())
        ]
        if len(devices) > 0:
            return "\n".join(devices)
        else:
            return "N/A"
    else:
        return "N/A"


def get_running_dpcpp_version(run_lambda):
    return run_and_read_all(
        run_lambda, 'env | grep CMPLR_ROOT | rev | cut -d "/" -f 1 | rev'
    )


def get_mkl_version(run_lambda):
    return run_and_read_all(
        run_lambda, 'env | grep MKLROOT | rev | cut -d "/" -f 1 | rev'
    )


def get_cpu_info(run_lambda):
    rc, out, err = 0, "", ""
    platform = get_platform()
    if platform == "linux":
        rc, out, err = run_lambda("lscpu")
    elif platform == "win32":
        rc, out, err = run_lambda(
            'powershell.exe "gwmi -Class Win32_Processor | Select-Object -Property Description,Manufacturer,Name,\
            NumberOfCores,NumberOfEnabledCore,NumberOfLogicalProcessors,ThreadCount | ConvertTo-Json"'
        )
        if rc == 0:
            lst = []
            try:
                obj = json.loads(out)
                if type(obj) is list:
                    for o in obj:
                        lst.append("----------------------")
                        for key in o.keys():
                            lst.append(f"{key}: {o[key]}")
                else:
                    for key in obj.keys():
                        lst.append(f"{key}: {obj[key]}")
            except ValueError as e:
                lst.append(out)
                lst.append(str(e))
            out = "\n".join(lst)
    elif platform == "darwin":
        rc, out, err = run_lambda("sysctl -n machdep.cpu.brand_string")
    cpu_info = "N/A"
    if rc == 0:
        cpu_info = out
    else:
        cpu_info = err
    return cpu_info


def get_platform():
    if sys.platform.startswith("linux"):
        return "linux"
    elif sys.platform.startswith("win32"):
        return "win32"
    elif sys.platform.startswith("cygwin"):
        return "cygwin"
    elif sys.platform.startswith("darwin"):
        return "darwin"
    else:
        return sys.platform


def get_mac_version(run_lambda):
    return run_and_parse_first_match(run_lambda, "sw_vers -productVersion", r"(.*)")


def get_windows_version(run_lambda):
    ret = run_and_read_all(
        run_lambda,
        'powershell.exe "gwmi -Class Win32_OperatingSystem | Select-Object -Property Caption,\
        OSArchitecture,Version | ConvertTo-Json"',
    )
    try:
        obj = json.loads(ret)
        ret = f'{obj["Caption"]} ({obj["Version"]} {obj["OSArchitecture"]})'
    except ValueError as e:
        ret += f"\n{str(e)}"
    return ret


def get_lsb_version(run_lambda):
    return run_and_parse_first_match(
        run_lambda, "lsb_release -a", r"Description:\t(.*)"
    )


def check_release_file(run_lambda):
    return run_and_parse_first_match(
        run_lambda, "cat /etc/*-release", r'PRETTY_NAME="(.*)"'
    )


def get_os(run_lambda):
    from platform import machine

    platform = get_platform()

    if platform == "win32" or platform == "cygwin":
        return get_windows_version(run_lambda)

    if platform == "darwin":
        version = get_mac_version(run_lambda)
        if version is None:
            return None
        return f"macOS {version} ({machine()})"

    if platform == "linux":
        # Ubuntu/Debian based
        desc = get_lsb_version(run_lambda)
        if desc is not None:
            return f"{desc} ({machine()})"

        # Try reading /etc/*-release
        desc = check_release_file(run_lambda)
        if desc is not None:
            return f"{desc} ({machine()})"

        return f"{platform} ({machine()})"

    # Unknown platform
    return platform


def get_python_platform():
    import platform

    return platform.platform()


def get_libc_version():
    import platform

    if get_platform() != "linux":
        return "N/A"
    return "-".join(platform.libc_ver())


def filter_python_packages(data):
    predefined_list = {
        "torch",
        "numpy",
        "intel",
        "mkl",
        "dpcpp",
        "ccl",
        "mpi",
        "pti",
        "transformers",
        "deepspeed",
        "libuv",
    }
    return [
        line
        for line in data.splitlines()
        if not line.startswith("#")
        and any(
            name in line
            for name in {
                "torch",
                "numpy",
                "intel",
                "mkl",
                "dpcpp",
                "ccl",
                "mpi",
                "pti",
                "transformers",
                "deepspeed",
                "libuv",
            }
        )
    ]


def get_python_packages(run_lambda):
    conda = os.environ.get("CONDA_EXE", "conda")
    pyenv = "conda"
    out = run_and_read_all(run_lambda, f"{conda} list")
    pkgs_filtered = []
    try:
        pkgs_filtered = filter_python_packages(out)
    except Exception:
        pass
    if len(pkgs_filtered) == 0:
        pyenv = "pip"
        out = run_and_read_all(
            run_lambda, f"{sys.executable} -mpip list --format=freeze"
        )
        pkgs_filtered = filter_python_packages(out)

    return pyenv, "\n".join(pkgs_filtered)


def get_env_info():
    run_lambda = run
    pyenv, pip_list_output = get_python_packages(run_lambda)

    if TORCH_AVAILABLE:
        torch_version_str = torch.__version__
        torch_cxx11_abi_str = torch._C._GLIBCXX_USE_CXX11_ABI
    else:
        torch_version_str = torch_cxx11_abi_str = "N/A"

    if IPEX_AVAILABLE:
        ipex_version_str = ipex.__version__
        try:
            import intel_extension_for_pytorch._version as ver
        except ImportError:
            import intel_extension_for_pytorch.version as ver
        try:
            ipex_gitrev_str = ver.__ipex_gitrev__
        except AttributeError:
            ipex_gitrev_str = ver.__gitrev__
        try:
            build_type_str = str(ver.__build_type__)
        except AttributeError:
            build_type_str = str(ver.__mode__)
        try:
            xpu_available_str = str(torch.xpu.is_available())
        except AttributeError:
            xpu_available_str = False
    else:
        ipex_version_str = ipex_gitrev_str = "N/A"
        build_type_str = xpu_available_str = "N/A"

    sys_version = sys.version.replace("\n", " ")

    return SystemEnv(
        torch_version=torch_version_str,
        torch_cxx11_abi=torch_cxx11_abi_str,
        ipex_version=ipex_version_str,
        ipex_gitrev=ipex_gitrev_str,
        build_type=build_type_str,
        python_version=f"{sys_version} ({sys.maxsize.bit_length() + 1}-bit runtime)",
        python_platform=get_python_platform(),
        is_xpu_available=xpu_available_str,
        dpcpp_runtime_version=get_running_dpcpp_version(run_lambda),
        mkl_version=get_mkl_version(run_lambda),
        gpu_models_onboard=f"\n{get_gpu_info_onboard(run_lambda)}",
        gpu_models_detected=f"\n{get_gpu_info_detected(run_lambda)}",
        driver_version=f"\n{get_driver_version(run_lambda)}",
        python_env=pyenv,
        python_packages=pip_list_output,
        os=get_os(run_lambda),
        libc_version=get_libc_version(),
        gcc_version=get_gcc_version(run_lambda),
        clang_version=get_clang_version(run_lambda),
        icx_version=get_icx_version(run_lambda),
        cmake_version=get_cmake_version(run_lambda),
        cpu_info=get_cpu_info(run_lambda),
    )


env_info_fmt = """
PyTorch version:   {torch_version}
PyTorch CXX11 ABI: {torch_cxx11_abi}
IPEX version:      {ipex_version}
IPEX commit:       {ipex_gitrev}
Build type:        {build_type}

OS:                {os}
GCC version:       {gcc_version}
Clang version:     {clang_version}
IGC version:       {icx_version}
CMake version:     {cmake_version}
Libc version:      {libc_version}

Python version:    {python_version}
Python platform:   {python_platform}
Is XPU available:  {is_xpu_available}
DPCPP runtime:     {dpcpp_runtime_version}
MKL version:       {mkl_version}

GPU models and configuration onboard: {gpu_models_onboard}

GPU models and configuration detected: {gpu_models_detected}

Driver version: {driver_version}

CPU:
{cpu_info}

Versions of relevant libraries:
{python_packages}
""".strip()


def pretty_str(envinfo):
    def replace_nones(dct, replacement="Could not collect"):
        for key in dct.keys():
            if dct[key] is not None:
                continue
            dct[key] = replacement
        return dct

    def replace_empties(dct, replacement="Could not collect"):
        for key in dct.keys():
            if dct[key] is not None and len(dct[key]) > 0:
                continue
            dct[key] = replacement
        return dct

    def replace_bools(dct, true="Yes", false="No"):
        for key in dct.keys():
            if dct[key] is True:
                dct[key] = true
            elif dct[key] is False:
                dct[key] = false
        return dct

    def prepend(text, tag="[prepend]"):
        lines = text.split("\n")
        updated_lines = [tag + line for line in lines]
        return "\n".join(updated_lines)

    def replace_if_empty(text, replacement="No relevant packages"):
        if text is not None and len(text) == 0:
            return replacement
        return text

    def maybe_start_on_next_line(string):
        # If `string` is multiline, prepend a \n to it.
        if string is not None and len(string.split("\n")) > 1:
            return f"\n{string}\n"
        return string

    mutable_dict = envinfo._asdict()

    # Replace True with Yes, False with No
    mutable_dict = replace_bools(mutable_dict)

    # Replace all None objects with 'N/A'
    mutable_dict = replace_nones(mutable_dict, replacement="N/A")

    # Replace all empty objects with 'N/A'
    mutable_dict = replace_empties(mutable_dict, replacement="N/A")

    # If either of these are '', replace with 'No relevant packages'
    mutable_dict["python_packages"] = replace_if_empty(mutable_dict["python_packages"])

    # Tag conda and pip packages with a prefix
    # If they were previously None, they'll show up as ie '[conda] Could not collect'
    if mutable_dict["python_packages"]:
        mutable_dict["python_packages"] = prepend(
            mutable_dict["python_packages"], f"[{envinfo.python_env}] "
        )
    mutable_dict["cpu_info"] = envinfo.cpu_info
    return env_info_fmt.format(**mutable_dict)


def get_pretty_env_info():
    return pretty_str(get_env_info())


def main():
    print("Collecting environment information...")
    output = get_pretty_env_info()
    print("=====================================")
    print(output)


if __name__ == "__main__":
    main()
