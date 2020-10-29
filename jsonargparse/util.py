"""Collection of general functions and classes."""

import os
import re
import sys
import stat
import logging
from copy import deepcopy
from typing import Dict, Any, Set
from contextlib import contextmanager, redirect_stderr
from argparse import Namespace

from .optionals import url_support, _import_requests, _import_url_validator


null_logger = logging.Logger('null')
null_logger.addHandler(logging.NullHandler())

meta_keys = {'__cwd__', '__path__'}


class ParserError(Exception):
    """Error raised when parsing a value fails."""
    pass


def _get_key_value(cfg, key):
    """Gets the value for a given key in a config object (dict or argparse.Namespace)."""
    def key_in_cfg(cfg, key):
        if isinstance(cfg, Namespace) and hasattr(cfg, key):
            return True
        elif isinstance(cfg, dict) and key in cfg:
            return True
        return False

    c = cfg
    k = key
    while '.' in key and not key_in_cfg(c, k):
        kp, k = k.split('.', 1)
        c = c[kp] if isinstance(c, dict) else getattr(c, kp)

    return c[k] if isinstance(c, dict) else getattr(c, k)


def _flat_namespace_to_dict(cfg_ns:Namespace) -> Dict[str, Any]:
    """Converts a flat namespace into a nested dictionary.

    Args:
        cfg_ns (argparse.Namespace): The configuration to process.

    Returns:
        dict: The nested configuration dictionary.
    """
    cfg_ns = deepcopy(cfg_ns)
    cfg_dict = {}
    for k, v in vars(cfg_ns).items():
        ksplit = k.split('.')
        if len(ksplit) == 1:
            if isinstance(v, list) and any([isinstance(x, Namespace) for x in v]):
                cfg_dict[k] = [namespace_to_dict(x) for x in v]
            elif isinstance(v, Namespace):
                cfg_dict[k] = vars(v)  # type: ignore
            elif not (v is None and k in cfg_dict):
                cfg_dict[k] = v
        else:
            kdict = cfg_dict
            for num, kk in enumerate(ksplit[:len(ksplit)-1]):
                if kk not in kdict or kdict[kk] is None:
                    kdict[kk] = {}  # type: ignore
                elif not isinstance(kdict[kk], dict):
                    raise ParserError('Conflicting namespace base: '+'.'.join(ksplit[:num+1]))
                kdict = kdict[kk]  # type: ignore
            if ksplit[-1] in kdict and kdict[ksplit[-1]] is not None:
                raise ParserError('Conflicting namespace base: '+k)
            if isinstance(v, list) and any([isinstance(x, Namespace) for x in v]):
                kdict[ksplit[-1]] = [namespace_to_dict(x) for x in v]
            elif not (v is None and ksplit[-1] in kdict):
                kdict[ksplit[-1]] = v
    return cfg_dict


def _dict_to_flat_namespace(cfg_dict:Dict[str, Any]) -> Namespace:
    """Converts a nested dictionary into a flat namespace.

    Args:
        cfg_dict (dict): The configuration to process.

    Returns:
        argparse.Namespace: The configuration namespace.
    """
    cfg_dict = deepcopy(cfg_dict)
    cfg_ns = {}

    def flatten_dict(cfg, base=None):
        for key, val in cfg.items():
            kbase = key if base is None else base+'.'+key
            if isinstance(val, dict) and val != {} and all(isinstance(k, str) for k in val.keys()):
                flatten_dict(val, kbase)
            else:
                cfg_ns[kbase] = val

    flatten_dict(cfg_dict)

    return Namespace(**cfg_ns)


def dict_to_namespace(cfg_dict:Dict[str, Any]) -> Namespace:
    """Converts a nested dictionary into a nested namespace.

    Args:
        cfg_dict (dict): The configuration to process.

    Returns:
        argparse.Namespace: The nested configuration namespace.
    """
    cfg_dict = deepcopy(cfg_dict)
    def expand_dict(cfg):
        for k, v in cfg.items():
            if isinstance(v, dict) and all(isinstance(k, str) for k in v.keys()):
                cfg[k] = expand_dict(v)
            elif isinstance(v, list):
                for nn, vv in enumerate(v):
                    if isinstance(vv, dict) and all(isinstance(k, str) for k in vv.keys()):
                        cfg[k][nn] = expand_dict(vv)
        return Namespace(**cfg)
    return expand_dict(cfg_dict)


def namespace_to_dict(cfg_ns:Namespace) -> Dict[str, Any]:
    """Converts a nested namespace into a nested dictionary.

    Args:
        cfg_ns (argparse.Namespace): The configuration to process.

    Returns:
        dict: The nested configuration dictionary.
    """
    cfg_ns = deepcopy(cfg_ns)
    def expand_namespace(cfg):
        cfg = dict(vars(cfg))
        for k, v in cfg.items():
            if isinstance(v, Namespace):
                cfg[k] = expand_namespace(v)
            elif isinstance(v, list):
                for nn, vv in enumerate(v):
                    if isinstance(vv, Namespace):
                        cfg[k][nn] = expand_namespace(vv)
        return cfg
    return expand_namespace(cfg_ns)


def strip_meta(cfg):
    """Removes all metadata keys from a configuration object.

    Args:
        cfg (argparse.Namespace or dict): The configuration object to strip.

    Returns:
        argparse.Namespace: The stripped configuration object.
    """
    cfg = deepcopy(cfg)
    if not isinstance(cfg, dict):
        cfg = namespace_to_dict(cfg)

    def strip_keys(cfg, base=None):
        del_keys = []
        for key, val in cfg.items():
            kbase = key if base is None else base+'.'+key
            if isinstance(val, dict):
                strip_keys(val, kbase)
            elif key in meta_keys:
                del_keys.append(key)
        for key in del_keys:
            del cfg[key]

    strip_keys(cfg)
    return cfg


def _check_unknown_kwargs(kwargs:Dict[str, Any], keys:Set[str]):
    """Checks whether a kwargs dict has unexpected keys.

    Args:
        kwargs (dict): The keyword arguments dict to check.
        keys (set): The expected keys.

    Raises:
        ValueError: If an unexpected keyword argument is found.
    """
    if len(set(kwargs.keys())-keys) > 0:
        raise ValueError('Unexpected keyword arguments: '+', '.join(set(kwargs.keys())-keys)+'.')


def usage_and_exit_error_handler(self, message):
    """Error handler to get the same behavior as in argparse.

    Args:
        self (ArgumentParser): The ArgumentParser object.
        message (str): The message describing the error being handled.
    """
    self.print_usage(sys.stderr)
    args = {'prog': self.prog, 'message': message}
    sys.stderr.write('%(prog)s: error: %(message)s\n' % args)
    sys.exit(2)


def _get_env_var(parser, action) -> str:
    """Returns the environment variable for a given parser and action."""
    env_var = (parser._env_prefix+'_' if parser._env_prefix else '') + action.dest
    env_var = env_var.replace('.', '__').upper()
    return env_var


@contextmanager
def _suppress_stderr():
    """A context manager that redirects stderr to devnull."""
    with open(os.devnull, 'w') as fnull:
        with redirect_stderr(fnull):
            yield None


class Path:
    """Stores a (possibly relative) path and the corresponding absolute path.

    When a Path instance is created it is checked that: the path exists, whether
    it is a file or directory and whether has the required access permissions
    (f=file, d=directory, r=readable, w=writeable, x=executable, c=creatable,
    u=url or in uppercase meaning not, i.e., F=not-file, D=not-directory,
    R=not-readable, W=not-writeable and X=not-executable). The absolute path can
    be obtained without having to remember the working directory from when the
    object was created.
    """
    def __init__(self, path, mode:str='fr', cwd:str=None, skip_check:bool=False):
        """Initializer for Path instance.

        Args:
            path (str or Path): The path to check and store.
            mode (str): The required type and access permissions among [fdrwxcuFDRWX].
            cwd (str): Working directory for relative paths. If None, then os.getcwd() is used.
            skip_check (bool): Whether to skip path checks.

        Raises:
            ValueError: If the provided mode is invalid.
            TypeError: If the path does not exist or does not agree with the mode.
        """
        self._check_mode(mode)
        if cwd is None:
            cwd = os.getcwd()

        if isinstance(cwd, list):
            cwd = cwd[0]  # Temporal until multiple cwds is implemented.

        is_url = False
        if isinstance(path, Path):
            is_url = path.is_url
            cwd = path.cwd  # type: ignore
            abs_path = path.abs_path  # type: ignore
            path = path.path  # type: ignore
        elif isinstance(path, str):
            abs_path = path
            if re.match('^file:///?', abs_path):
                abs_path = re.sub('^file:///?', '/', abs_path)
            if 'u' in mode and url_support and _import_url_validator('Path')(abs_path):  # type: ignore
                is_url = True
            elif 'f' in mode or 'd' in mode:
                abs_path = abs_path if os.path.isabs(abs_path) else os.path.join(cwd, abs_path)
        else:
            raise TypeError('Expected path to be a string or a Path object.')

        if not skip_check and is_url:
            if 'r' in mode:
                requests = _import_requests('Path with URL support')
                try:
                    requests.head(abs_path).raise_for_status()  # type: ignore
                except requests.HTTPError as ex:
                    raise TypeError(abs_path+' HEAD not accessible :: '+str(ex))
        elif not skip_check:
            ptype = 'Directory' if 'd' in mode else 'File'
            if 'c' in mode:
                pdir = os.path.realpath(os.path.join(abs_path, '..'))
                if not os.path.isdir(pdir):
                    raise TypeError(ptype+' is not creatable since parent directory does not exist: '+abs_path)
                if not os.access(pdir, os.W_OK):
                    raise TypeError(ptype+' is not creatable since parent directory not writeable: '+abs_path)
                if 'd' in mode and os.access(abs_path, os.F_OK) and not os.path.isdir(abs_path):
                    raise TypeError(ptype+' is not creatable since path already exists: '+abs_path)
                if 'f' in mode and os.access(abs_path, os.F_OK) and not os.path.isfile(abs_path):
                    raise TypeError(ptype+' is not creatable since path already exists: '+abs_path)
            else:
                if not os.access(abs_path, os.F_OK):
                    raise TypeError(ptype+' does not exist: '+abs_path)
                if 'd' in mode and not os.path.isdir(abs_path):
                    raise TypeError('Path is not a directory: '+abs_path)
                if 'f' in mode and not (os.path.isfile(abs_path) or stat.S_ISFIFO(os.stat(abs_path).st_mode)):
                    raise TypeError('Path is not a file: '+abs_path)
            if 'r' in mode and not os.access(abs_path, os.R_OK):
                raise TypeError(ptype+' is not readable: '+abs_path)
            if 'w' in mode and not os.access(abs_path, os.W_OK):
                raise TypeError(ptype+' is not writeable: '+abs_path)
            if 'x' in mode and not os.access(abs_path, os.X_OK):
                raise TypeError(ptype+' is not executable: '+abs_path)
            if 'D' in mode and os.path.isdir(abs_path):
                raise TypeError('Path is a directory: '+abs_path)
            if 'F' in mode and (os.path.isfile(abs_path) or stat.S_ISFIFO(os.stat(abs_path).st_mode)):
                raise TypeError('Path is a file: '+abs_path)
            if 'R' in mode and os.access(abs_path, os.R_OK):
                raise TypeError(ptype+' is readable: '+abs_path)
            if 'W' in mode and os.access(abs_path, os.W_OK):
                raise TypeError(ptype+' is writeable: '+abs_path)
            if 'X' in mode and os.access(abs_path, os.X_OK):
                raise TypeError(ptype+' is executable: '+abs_path)

        self.path = path
        self.abs_path = abs_path
        self.cwd = cwd
        self.mode = mode
        self.is_url = is_url  # type: bool

    def __str__(self):
        return self.abs_path

    def __repr__(self):
        return 'Path(path="'+self.path+'", abs_path="'+self.abs_path+'", cwd="'+self.cwd+'")'

    def __call__(self, absolute=True):
        """Returns the path as a string.

        Args:
            absolute (bool): If false returns the original path given, otherwise the corresponding absolute path.
        """
        return self.abs_path if absolute else self.path

    def get_content(self, mode='r'):
        """Returns the contents of the file or the response of a GET request to the URL."""
        if not self.is_url:
            with open(self.abs_path, mode) as input_file:
                return input_file.read()
        else:
            requests = _import_requests('Path with URL support')
            response = requests.get(self.abs_path)
            response.raise_for_status()
            return response.text

    @staticmethod
    def _check_mode(mode:str):
        if not isinstance(mode, str):
            raise ValueError('Expected mode to be a string.')
        if len(set(mode)-set('fdrwxcuFDRWX')) > 0:
            raise ValueError('Expected mode to only include [fdrwxcuFDRWX] flags.')
        if 'f' in mode and 'd' in mode:
            raise ValueError('Both modes "f" and "d" not possible.')
        if 'u' in mode and 'd' in mode:
            raise ValueError('Both modes "d" and "u" not possible.')


class LoggerProperty:
    """Class designed to be inherited by other classes to add a logger property."""

    def __init__(self):
        """Initializer for LoggerProperty class."""
        if not hasattr(self, '_logger'):
            self.logger = None


    @property
    def logger(self):
        """The current logger."""
        return self._logger


    @logger.setter
    def logger(self, logger):
        """Sets a new logger.

        Args:
            logger (logging.Logger or bool or str or dict or None): A logger
                object to use, or True/str(logger name)/dict(name, level) to use the
                default logger, or False/None to disable logging.

        Raises:
            ValueError: If an invalid logger value is given.
        """
        if logger is None or (isinstance(logger, bool) and not logger):
            self._logger = null_logger
        elif isinstance(logger, (bool, str, dict)) and logger:
            levels = {'CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG'}
            level = logging.INFO
            if isinstance(logger, dict) and 'level' in logger:
                if logger['level'] not in levels:
                    raise ValueError('Logger level must be one of '+str(levels)+'.')
                level = getattr(logging, logger['level'])
            name = type(self).__name__
            if isinstance(logger, str):
                name = logger
            elif isinstance(logger, dict) and 'name' in logger:
                name = logger['name']
            logger = logging.getLogger(name)
            if len(logger.handlers) == 0:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
                logger.addHandler(handler)
            logger.setLevel(level)
            self._logger = logger
        elif not isinstance(logger, logging.Logger):
            raise ValueError('Expected logger to be an instance of logging.Logger or bool or str or dict or None.')
        else:
            self._logger = logger