#!/usr/bin/env python3

import os
import sys
import git
import fire
import yaml
import utils
import shutil
import tomllib
import subprocess
from loguru import logger
from pathlib import Path
from sysroot import Sysroot
from package import Package


class GitProgress(git.RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        logger.trace(f"cloning {cur_count}/{max_count} {message}")


def ndk_vulkan_include(toolchain):
    # NDK sysroot canonical Vulkan headers (includes vulkan_android.h with
    # VkAndroidHardwareBufferUsageANDROID and other platform types).
    return Path(toolchain, 'sysroot', 'usr', 'include')


def termux_stubs_dir():
    # Stub headers for Android platform-internal APIs absent from the NDK
    # (hardware/hwvulkan.h, vndk/hardware_buffer.h, vulkan/vk_android_native_buffer.h).
    return Path(__file__).parent / 'stubs'


def gn_list(items):
    quoted = ', '.join(f'"{it}"' for it in items)
    return f'[{quoted}]'


@utils.record
class Build:
    @utils.recordm
    def __init__(self, conf='build.toml'):
        path = Path(__file__).parent
        conf = path/conf

        with open(conf, 'rb') as f:
            cfg = tomllib.load(f)

        ndk = cfg['ndk'].get('path') or os.environ.get('ANDROID_NDK')
        api = cfg['ndk'].get('api')
        tag = cfg['flutter'].get('tag')
        repo = cfg['flutter'].get('repo')
        root = cfg['flutter'].get('path')
        arch = cfg['build'].get('arch')
        mode = cfg['build'].get('runtime')
        gclient = cfg['build'].get('gclient')
        sysroot = cfg['sysroot']
        syspath = sysroot.pop('path')
        package = cfg['package'].get('conf')
        release = cfg['package'].get('path')
        patches = cfg.get('patch')

        if not ndk:
            raise ValueError('neither ndk path nor ANDROID_NDK is set')
        if not tag:
            raise ValueError('require flutter tag')

        # TODO: check parameters
        self.tag = tag
        self.dart_version = cfg['flutter'].get('dart_version') or ''
        self.framework_revision = cfg['flutter'].get('framework_revision') or ''
        self.framework_commit_date = cfg['flutter'].get('framework_commit_date') or ''
        self.devtools_version = cfg['flutter'].get('devtools_version') or ''
        self.ndk_version = cfg['ndk'].get('version') or ''
        self.compile_sdk = cfg['android'].get('compile_sdk')
        self.target_sdk = cfg['android'].get('target_sdk')
        self.api = api or 26
        self.conf = conf
        # TODO: detect host
        self.host = 'linux-x86_64'
        self.repo = repo or 'https://github.com/flutter/flutter'
        self.arch = arch or 'arm64'
        self.mode = mode or 'debug'
        self.sysroot = Sysroot(path=path/syspath, **sysroot)
        self.root = path/root
        self.gclient = path/gclient
        self.release = path/release
        self.toolchain = Path(ndk, f'toolchains/llvm/prebuilt/{self.host}')

        if not self.release.parent.is_dir():
            raise ValueError(f'bad release path: "{release}"')

        with open(path/package, 'rb') as f:
            self.package = yaml.safe_load(f)

        if isinstance(patches, dict):
            self.patches = {}

            def patch(key):
                return lambda: self.patch(**self.patches[key])

            for k, v in patches.items():
                self.patches[k] = {
                    'file': path/v['file'],
                    'path': self.root/v['path']}
                self.__dict__[f'patch_{k}'] = patch(k)

    def config(self):
        info = (f'{k}\t: {v}' for k, v in self.__dict__.items() if k != 'package')
        logger.info('\n'+'\n'.join(info))

    def clone(self, *, url: str = None, tag: str = None, out: str = None):
        url = url or self.repo
        out = out or self.root
        tag = tag or self.tag
        progress = GitProgress()

        if utils.flutter_tag(out) == tag:
            logger.info('flutter exists, skip.')
            return
        elif os.path.isdir(out):
            logger.info(f'moving {out} to {out}.old ...')
            os.rename(out, f'{out}.old')

        try:
            git.Repo.clone_from(
                url=url,
                to_path=out,
                progress=progress,
                branch=tag)
        except git.exc.GitCommandError:
            raise RuntimeError('\n'.join(progress.error_lines))

    def sync(self, *, cfg: str = None, root: str = None):
        cfg = cfg or self.gclient
        src = root or self.root

        shutil.copy(cfg, os.path.join(src, '.gclient'))
        cmd = ['gclient', 'sync', '-DR', '--no-history']
        subprocess.run(cmd, cwd=src, check=True, stdout=True, stderr=True)

    def patch(self, *, file, path):
        repo = git.Repo(path)
        repo.git.apply([file])

    def configure(
        self,
        arch: str,
        mode: str,
        api: int = None,
        root: str = None,
        sysroot: str = None,
        toolchain: str = None,
    ):
        root = root or self.root
        api = api or self.api
        sysroot = os.path.abspath(sysroot or self.sysroot.path)
        toolchain = os.path.abspath(toolchain or self.toolchain)
        vulkan = ndk_vulkan_include(toolchain)
        stubs = termux_stubs_dir()
        cmd = [
            'vpython3',
            'engine/src/flutter/tools/gn',
            '--linux',
            '--linux-cpu', arch,
            '--enable-fontconfig',
            '--no-goma',
            '--no-backtrace',
            '--clang',
            '--lto',
            '--no-enable-unittests',
            '--no-build-embedder-examples',
            '--no-prebuilt-dart-sdk',
            '--target-toolchain', toolchain,
            '--runtime-mode', mode,
            '--no-build-glfw-shell',
            '--gn-args', 'symbol_level=0',
            '--gn-args', 'arm_use_neon=false',
            '--gn-args', 'arm_optionally_use_neon=true',
            '--gn-args', 'dart_include_wasm_opt=false',
            '--gn-args', 'dart_platform_sdk=false',
            '--gn-args', 'is_desktop_linux=false',
            '--gn-args', 'use_default_linux_sysroot=false',
            '--gn-args', 'skia_use_perfetto=false',
            '--gn-args', f'custom_sysroot="{sysroot}"',
            '--gn-args', 'is_termux=true',
            '--gn-args', f'is_termux_host={utils.__TERMUX__}',
            '--gn-args', f'termux_api_level={api}',
            '--gn-args', 'extra_ldflags=["-lEGL", "-lGLESv2", "-llog"]',
            # Provide stub headers for Android platform-internal APIs that are
            # not part of the public NDK (e.g. vk_android_native_buffer.h,
            # hardware/hwvulkan.h, vndk/hardware_buffer.h).
            # -D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__: NDK headers guard
            #   API-level-gated functions with __BIONIC_AVAILABILITY(strict,...).
            #   Defining this macro before any NDK headers are included changes
            #   the mode from "hard error" to "weak import", allowing SwiftShader
            #   to call API-29 functions (e.g. AHardwareBuffer_lockPlanes) even
            #   when targeting API 26.  Termux runs on Android where these
            #   symbols are present, so the weak-import behaviour is correct.
            # -Wno-newline-eof: suppress warning on third-party headers
            #   (SwiftShader) that legitimately lack a trailing newline.
            '--gn-args',
            f'extra_cflags={gn_list([f"-I{vulkan}", f"-I{stubs}", "-D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__"])}',
            '--gn-args',
            f'extra_cflags_cc={gn_list([f"-I{vulkan}", f"-I{stubs}", "-D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__", "-Wno-newline-eof"])}',
        ]
        subprocess.run(cmd, cwd=root, check=True, stdout=True, stderr=True)

    def build(self, arch: str, mode: str, root: str = None, jobs: int = None):
        root = root or self.root
        cmd = [
            'ninja', '-C', utils.target_output(root, arch, mode),
            'flutter',
            'flutter/build/archives:artifacts',
            'flutter/build/archives:dart_sdk_archive',
            'flutter/build/archives:flutter_patched_sdk',
            'flutter/shell/platform/linux:flutter_gtk',
            'flutter/tools/font_subset',
        ]
        if jobs:
            cmd.append(f'-j{jobs}')
        subprocess.run(cmd, check=True, stdout=True, stderr=True)

    def _patch_android_host(self, root: str):
        """Let ANDROID builds compile host tools (gen_snapshot) for Termux.

        The termux host_toolchain defaults to //build/toolchain/termux:$host_cpu
        which on x86-64 CI builders means bionic x86_64.  Override the cpu via a
        new termux_host_cpu gn arg so GitHub-hosted (x64) runners can emit a
        native ARM64 bionic gen_snapshot.
        """
        src = Path(root) / 'engine' / 'src' / 'build'
        gni = src / 'config' / 'termux' / 'termux.gni'
        bc = src / 'config' / 'BUILDCONFIG.gn'

        gni_old = '  is_termux = false\n  is_termux_host = false\n}'
        gni_new = '  is_termux = false\n  is_termux_host = false\n\n  termux_host_cpu = ""\n}'
        s = gni.read_text()
        if gni_new not in s:
            assert gni_old in s, f'unexpected termux.gni content in {gni}'
            gni.write_text(s.replace(gni_old, gni_new))
            logger.info('patched termux.gni: termux_host_cpu')

        bc_old = ('if (is_termux_host) {\n'
                  '  host_toolchain = "//build/toolchain/termux:$host_cpu"\n'
                  '}')
        bc_new = ('if (is_termux_host) {\n'
                  '  if (termux_host_cpu != "") {\n'
                  '    host_toolchain = "//build/toolchain/termux:$termux_host_cpu"\n'
                  '  } else {\n'
                  '    host_toolchain = "//build/toolchain/termux:$host_cpu"\n'
                  '  }\n'
                  '}')
        s = bc.read_text()
        if bc_new not in s:
            assert bc_old in s, f'unexpected BUILDCONFIG.gn content in {bc}'
            bc.write_text(s.replace(bc_old, bc_new))
            logger.info('patched BUILDCONFIG.gn: termux_host_cpu host_toolchain')

    def configure_android(
        self,
        arch: str = 'arm64',
        mode: str = 'release',
        root: str = None,
        sysroot: str = None,
        toolchain: str = None,
    ):
        """Configure an Android build whose host tools are Termux-native.

        Produces out/android_release_<arch>/clang_arm64/gen_snapshot as a
        bionic ARM64 executable runnable directly on Termux.
        """
        root = root or self.root
        sysroot = os.path.abspath(sysroot or self.sysroot.path)
        toolchain = os.path.abspath(toolchain or self.toolchain)
        self._patch_android_host(root)
        cmd = [
            'vpython3',
            'engine/src/flutter/tools/gn',
            '--android',
            '--android-cpu', arch,
            '--no-goma',
            '--no-backtrace',
            '--clang',
            '--lto',
            '--no-enable-unittests',
            '--no-build-embedder-examples',
            '--no-prebuilt-dart-sdk',
            '--runtime-mode', mode,
            '--target-toolchain', toolchain,
            '--gn-args', 'symbol_level=0',
            '--gn-args', 'dart_include_wasm_opt=false',
            '--gn-args', 'skia_use_perfetto=false',
            '--gn-args', 'is_desktop_linux=false',
            '--gn-args', 'use_default_linux_sysroot=false',
            '--gn-args', f'custom_sysroot="{sysroot}"',
            '--gn-args', f'target_sysroot="{sysroot}"',
            '--gn-args', 'is_termux=true',
            '--gn-args', 'is_termux_host=true',
            '--gn-args', f'termux_host_cpu="{arch}"',
            '--gn-args', f'termux_api_level={self.api}',
        ]
        logger.info(f'configure android ({mode}/{arch}) -> {cmd}')
        subprocess.run(cmd, cwd=root, check=True, stdout=True, stderr=True)

    def build_android_gen_snapshot(self, root: str = None, jobs: int = None):
        root = root or self.root
        out = os.path.join(root, 'engine', 'src', 'out', 'android_release_arm64')
        cmd = [
            'ninja', '-C', out,
            'flutter/third_party/dart/runtime/bin:gen_snapshot',
        ]
        if jobs:
            cmd.append(f'-j{jobs}')
        logger.info(f'building android gen_snapshot: {" ".join(cmd)}')
        subprocess.run(cmd, check=True, stdout=True, stderr=True)

        out = Path(out)
        host = out / 'host'
        host.mkdir(exist_ok=True)
        for cand in (
            out / 'host' / 'gen_snapshot',
            out / 'clang_arm64' / 'gen_snapshot',
            out / 'clang_x64' / 'gen_snapshot',
            out / 'gen_snapshot',
        ):
            if cand.is_file():
                dst = host / 'gen_snapshot'
                if cand != dst:
                    shutil.copy(cand, dst)
                logger.info(f'android gen_snapshot -> {dst}')
                return dst
        raise RuntimeError(f'gen_snapshot not found under {out}')

    def debuild(self, arch: str, output: str = None, root: str = None, **conf):
        conf = conf or self.package
        root = root or self.root
        output = output or self.output(arch)

        pkg = Package(
            root=root,
            arch=arch,
            dart_version=self.dart_version,
            framework_revision=self.framework_revision,
            framework_commit_date=self.framework_commit_date,
            devtools_version=self.devtools_version,
            ndk_version=self.ndk_version,
            compile_sdk=self.compile_sdk,
            target_sdk=self.target_sdk,
            **conf)
        pkg.debuild(output=output)

    def output(self, arch: str):
        if self.release.is_dir():
            name = f'flutter_{self.tag}_{utils.termux_arch(arch)}.deb'
            return self.release/name
        else:
            return self.release

    # TODO: check gclient and ninja existence
    def __call__(self):
        self.config()
        self.clone()
        self.sync()

        for arch in self.arch:
            self.sysroot(arch=arch)
            for mode in self.mode:
                self.configure(arch=arch, mode=mode)
                self.build(arch=arch, mode=mode)
            if str(arch) == 'arm64':
                self.configure_android(arch=arch)
                self.build_android_gen_snapshot(arch=arch)
            self.debuild(arch=arch, output=self.output(arch))


if __name__ == '__main__':
    logger.remove()
    logger.add(
        sys.stdout,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <9}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>")
        )
    fire.Fire(Build())
