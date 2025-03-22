#!/usr/bin/env python3

"""
Copyright 2025 Aidan Ocmer

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import sys
import urllib.request
import gzip
import os
import json


class Conf:
    def __init__(self, file):
        self.data = json.load(file)

    def __getattr__(self, attr: str):
        return self.data[attr]


config: Conf

dotconf: str = (
    os.environ["XDG_CONFIG_HOME"]
    if "XDG_CONFIG_HOME" in os.environ
    else os.path.join(os.environ["HOME"], ".config")
)

dotcache: str = (
    os.environ["XDG_CACHE_HOME"]
    if "XDG_CACHE_HOME" in os.environ
    else os.path.join(os.environ["HOME"], ".cache")
)

conffolder = os.path.join(dotconf, "debdl")
conffile = os.path.join(conffolder, "config.json")

if not os.path.isdir(conffolder):
    os.mkdir(conffolder)

if not os.path.exists(conffile):
    with open(conffile, "w") as fw:
        json.dump(
            {
                "mirror": "http://ftp.debian.org/debian",
                "dist": "stable",
                "component": "main",
                "architecture": "binary-amd64",
            },
            fw,
        )

with open(conffile, "r") as f:
    config = Conf(f)

# URL to the Packages file for the specified distribution, component, and architecture
PACKAGES_URL = f"{config.mirror}/dists/{config.dist}/{config.component}/{config.architecture}/Packages.gz"
LOCAL_PACKAGES_FILE = f"{dotcache}/Packages.gz"
INSTALL_SCRIPT = "install.sh"


def download_packages_file():
    """Download the Packages.gz file if not already cached."""
    if not os.path.exists(LOCAL_PACKAGES_FILE):
        print("Downloading Packages.gz ...")
        urllib.request.urlretrieve(PACKAGES_URL, LOCAL_PACKAGES_FILE)
    else:
        print("Using cached Packages.gz")


def parse_packages_file():
    """
    Parse the downloaded Packages.gz file and return a dictionary mapping
    package names to their metadata.
    """
    packages = {}
    with gzip.open(LOCAL_PACKAGES_FILE, "rt", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    entries = content.split("\n\n")
    for entry in entries:
        if not entry.strip():
            continue
        pkg_info = {}
        lines = entry.splitlines()
        key = None
        for line in lines:
            # Handle line continuations
            if line.startswith(" "):
                if key:
                    pkg_info[key] += " " + line.strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                pkg_info[key] = value
        if "Package" in pkg_info:
            packages[pkg_info["Package"]] = pkg_info
    return packages


def parse_dependencies(dep_str):
    """
    Given a dependency string from the Packages file, return a list of package names.
    For example, a string like:
      "libc6 (>= 2.29), libgcc1 (>= 1:3.0) | libgcc-s1"
    will yield ['libc6', 'libgcc1'].
    This function splits on commas and for alternatives (separated by '|') chooses the first.
    """
    deps = []
    for dep in dep_str.split(","):
        dep = dep.strip()
        if not dep:
            continue
        # Split alternatives (if any) and choose the first
        alternatives = dep.split("|")
        first_alt = alternatives[0].strip()
        # Remove any version constraints (anything in parentheses)
        pkg = first_alt.split(" ")[0]
        deps.append(pkg)
    return deps


def resolve_dependencies(package_name, packages, resolved=None, seen=None):
    """
    Recursively resolve dependencies for a given package.
    Returns a set of package names that includes the package and all its dependencies.
    """
    if resolved is None:
        resolved = set()
    if seen is None:
        seen = set()
    if package_name in resolved:
        return resolved
    if package_name in seen:
        return resolved
    seen.add(package_name)
    if package_name not in packages:
        print(f"Warning: {package_name} not found in package list")
        return resolved
    pkg_info = packages[package_name]
    if "Depends" in pkg_info:
        dep_list = parse_dependencies(pkg_info["Depends"])
        for dep in dep_list:
            resolve_dependencies(dep, packages, resolved, seen)
    resolved.add(package_name)
    return resolved


def download_deb(package_name, packages, output_dir):
    """
    Download the .deb file for the given package using its Filename field.
    """
    if package_name not in packages:
        print(f"Package {package_name} not found in repository!")
        return
    pkg_info = packages[package_name]
    if "Filename" not in pkg_info:
        print(f"No Filename info for package {package_name}")
        return
    deb_url = f"{config.mirror}/{pkg_info['Filename']}"
    os.makedirs(output_dir, exist_ok=True)
    deb_path = os.path.join(output_dir, os.path.basename(pkg_info["Filename"]))
    if os.path.exists(deb_path):
        print(f"{deb_path} already exists, skipping download.")
        return
    print(f"Downloading {package_name} from {deb_url}")
    try:
        urllib.request.urlretrieve(deb_url, deb_path)
    except Exception as e:
        print(f"Error downloading {package_name}: {e}")


def compute_install_order(resolved, packages):
    """
    Compute an installation order (list) so that dependencies are installed before
    dependents. This function performs a DFS topological sort on the resolved set.
    """
    order = []
    visited = set()

    def dfs(pkg):
        if pkg in visited:
            return
        visited.add(pkg)
        if pkg in packages and "Depends" in packages[pkg]:
            for dep in parse_dependencies(packages[pkg]["Depends"]):
                if dep in resolved:
                    dfs(dep)
        order.append(pkg)

    # Process each package in the resolved set.
    for pkg in resolved:
        if pkg not in visited:
            dfs(pkg)
    return order


def generate_install_script(
    install_order, packages, output_dir, script_name=INSTALL_SCRIPT
):
    """
    Generate a shell script that installs the .deb files in the given order.
    The script assumes that the .deb files are located in output_dir.
    """
    lines = [
        "#!/bin/bash",
        "set -e",  # Stop on error
        "",
        "echo 'Starting installation of downloaded .deb packages...'",
        "",
    ]
    for pkg in install_order:
        if pkg not in packages or "Filename" not in packages[pkg]:
            continue
        deb_file = os.path.basename(packages[pkg]["Filename"])
        deb_path = os.path.join(output_dir, deb_file)
        # Add a line to install this deb package
        lines.append(f"echo 'Installing {pkg}...'")
        lines.append(f"sudo apt install ./{deb_path} || true")
        lines.append("")
    # Optionally, fix any dependency issues at the end.
    lines.append("echo 'Fixing dependencies, if any...'")
    lines.append("sudo apt-get install -f -y")
    lines.append("echo 'Installation complete.'")

    script_content = "\n".join(lines)
    with open(script_name, "w") as f:
        f.write(script_content)
    os.chmod(script_name, 0o755)
    print(f"Installation script generated: {script_name}")


def main():
    if len(sys.argv) < 2:
        print("Usage: debdl [-h --help] PACKAGES...\n")
        sys.exit(1)

    if sys.argv[1] == "--help" or sys.argv[1] == "-h":
        print("Usage: debdl [-h --help] PACKAGES...\n")
        print(
            "  --help -h\tShows this message",
            "\tChanging architecture output\n"
            f'To change architectures edit "{conffile}" and change the value of "architecture" to any of the list below:',
            " - binary-all\n - binary-amd64\n - binary-arm64\n - binary-armel\n - binary-armf\n - binary-i386\n - binary-mips64el\n - binary-mipsel\n - binary-ppc64el\n - binary-s390x\n",
            "Downloads .deb files from the Debian Package Repository regardless of distribution",
            "For bugs, make an issue at github (https://github.com/j3h1/debdl)",
            sep="\n\n",
        )

        sys.exit(1)

    target_packages = sys.argv[1:]

    print(f"NOTE: INSTALLING FROM {config.architecture} FOR MORE INFO RUN debdl --help")

    download_packages_file()
    packages = parse_packages_file()

    for package in target_packages:
        print("Resolving dependencies...")
        resolved = resolve_dependencies(package, packages)

        if resolved:
            print("Packages to download:")
            for pkg in resolved:
                print(f" - {pkg}")

            for pkg in resolved:
                download_deb(pkg, packages, package)

            install_order = compute_install_order(resolved, packages)
            print("Installation order:")
            for pkg in install_order:
                print(f" - {pkg}")

            generate_install_script(
                install_order,
                packages,
                package,
                package + "/" + INSTALL_SCRIPT,
            )


if __name__ == "__main__":
    main()
