#!python

import sys
import os
import time
import glob
import subprocess
from datetime import datetime

import toml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


### autogen.toml 内容例子
#
# [[dot2png]]
# 
# src = "a.dot"
# dst = "a.png"
# 
# [[dot2png]]
# 
# src = "b.dot"
# dst = "b.png"
# 
# [[dot2svg]]
# 
# src = "b.dot"
# dst = "b.svg"
AUTOGEN_CNF_NAME = 'autogen.toml'


class AutoGenMeta(type):

    # 收集所有 AutoGen 非 Base 子类
    registry = {}

    def __new__(cls, name, base, attrs):
        ret = super().__new__(cls, name, base, attrs)
        if not name.endswith('Base'):
            AutoGenMeta.registry[name.lower()] = ret
        return ret


class AutoGenBase(object, metaclass=AutoGenMeta):

    def __init__(self, dir, src, dst, **opts):
        '''构造一个 AutoGen

        Args:
            dir: 是基础目录路径，下面的文件名都是相对它的
            src: 一个或一系列源文件相对路径
            dst: 目标文件相对路径
            opts: 其他参数
        '''
        # 路径是
        abspath = lambda p: os.path.abspath(os.path.join(dir, p))
        if not isinstance(src, list):
            src = [src]
        assert len(src) > 0, 'No src at all'
        assert all((isinstance(s, str) for s in src)), 'src should be str or list of str'
        self.src = [abspath(p) for p in src]
        assert isinstance(dst, str), 'dst should be str'
        self.dst = abspath(dst)
        self.opts = opts

    def getmtime(self, path):
        '''获得某个文件的 mtime

        Args:
            path: 路径
        Returns:
            mtime 或 0 （如果文件不存在）
        '''
        try:
            return os.path.getmtime(path)
        except FileNotFoundError:
            return 0
    
    def __call__(self):
        '''如果有任意源文件的 mtime 比目标文件的新，则执行 gen
        '''
        src_mtime = map(self.getmtime, self.src)
        dst_mtime = self.getmtime(self.dst)
        # 如果所有源文件的 mtime 都小于等于目标文件的话，则不需要进行 autogen
        if all((t <= dst_mtime for t in src_mtime)):
            return

        print(self.__class__.__name__.lower())
        print('  %r' % self.src)
        print('  %r' % self.dst)
        t = datetime.now()
        try:
            r = self.gen()
        except Exception as e:
            r = str(e)
        dur = datetime.now() - t
        if not r:
            print('  Success in %s sec' % dur.total_seconds())
            return
        print('  Error in %s sec' % dur.total_seconds())
        print('\n'.join(('    ' + line for line in r.split('\n'))))

    def gen(self):
        '''实际执行，子类必须重载

        Returns:
            若成功返回空字符串，若失败返回错误信息
        '''


class ShellAutoGenBase(AutoGenBase):

    def gen(self):
        try:
            subprocess.check_output(self.shell_cmd(), stderr=subprocess.STDOUT, shell=True)
            return ''
        except subprocess.CalledProcessError as e:
            return e.output.decode('utf-8')

    def shell_cmd(self):
        pass


class Dot2PNG(ShellAutoGenBase):
    '''需要安装 graphviz (dot)'''
    def shell_cmd(self):
        return 'dot -Tpng -o %s %s' % (self.dst, self.src[0])


class Ipe2SVG(ShellAutoGenBase):
    '''需要安装 ipe/inkscape'''
    def shell_cmd(self):
        return ("iperender -svg %s /dev/stdout | "
            "inkscape -lp --export-filename=- --actions='select-all:all;clone-unlink-recursively' --vacuum-defs | "
            "sed 's/rgb(0\\%%,0\\%%,0\\%%)/currentColor/g' > %s") % (self.src[0], self.dst)


class Ipe2PNG(ShellAutoGenBase):
    '''需要安装 ipe'''
    def shell_cmd(self):
        return "iperender -png -resolution 256 %s %s" % (self.src[0], self.dst)


class SVG2PNG(ShellAutoGenBase):
    '''需要安装 inkscape'''
    def shell_cmd(self):
        return "inkscape --export-type=png --export-filename=%s %s" % (self.dst, self.src[0])


class MD2Ipynb(ShellAutoGenBase):
    '''需要安装 pandoc'''
    def shell_cmd(self):
        return "pandoc -f markdown+autolink_bare_uris+auto_identifiers+emoji -t ipynb -o %s %s" % (self.dst, self.src[0])


# src path -> set of AutoGen instances
# 表示当一个 src 文件更新后需要执行的 autogen 集合
_src_to_ags = {}


# cnf path -> list of AutoGen instances
# 表示一个 cnf 文件中定义的 autogen 集合
_cnf_to_ags = {}


def _cnf_modified(p):
    # 读取 conf 配置，p 应当是绝对路径
    dir = os.path.dirname(p)
    ags = []
    print('conf %s' % p)
    try:
        conf = toml.load(p)
        for name, lst in conf.items():
            cls = AutoGenMeta.registry[name.lower()]
            for opts in lst:
                ags.append(cls(dir, **opts))
    except Exception as e:
        print('  Exception %r' % e)
        return

    # 删除旧 conf 对应的 autogens
    for ag in _cnf_to_ags.get(p, []):
        for src in ag.src:
            _src_to_ags[src].discard(ag)
            if len(_src_to_ags[src]) == 0:
                del _src_to_ags[src]

    # 添加新 conf 对应的 autogens
    for ag in ags:
        for src in ag.src:
            _src_to_ags.setdefault(src, set()).add(ag)
    _cnf_to_ags[p] = ags
    print('  Successfully read')

    # 执行一次新的 autogens
    for ag in ags:
        ag()


def _src_modified(p):
    for ag in _src_to_ags.get(p, set()):
        ag()


def file_modified(p):
    if os.path.basename(p) == AUTOGEN_CNF_NAME:
        _cnf_modified(p)
        return
    _src_modified(p)


class AutoGenEventHandler(FileSystemEventHandler):

    def on_moved(self, event):
        if event.is_directory:
            return
        file_modified(event.dest_path)

    def on_closed(self, event):
        if event.is_directory:
            return
        file_modified(event.src_path)


def watch_dir(root):
    handler = AutoGenEventHandler()
    observer = Observer()
    observer.schedule(handler, root, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def init_watch_dir(root):
    for p in glob.glob(os.path.join(root, '**', AUTOGEN_CNF_NAME), recursive=True):
        _cnf_modified(p)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: autogen.py <root-dir>')
        sys.exit(1)
    root = os.path.abspath(sys.argv[1])
    if not os.path.isdir(root):
        print('%s is not a dir' % root)
        sys.exit(1)
    
    init_watch_dir(root)
    watch_dir(root)
