#!/usr/bin/env python3

from os import path, makedirs, chmod
import io
import jinja2
import sys
import yaml
import argparse
import subprocess
import requests
import tarfile
import logging

logger = logging.getLogger(__name__)

wildcard = '*'
defaults = {
    'eve': False,
    'domain': None,
    'challenges_directory': './challenges',
    'challenges': {
        '*': {
            'port': 1194,
            'files': []
        }
    },
    'registrar': {
        'port': 3960,
        'network': 'default'
    }
}

EASYRSA_URL='https://github.com/OpenVPN/easy-rsa/releases/download/v3.0.4/EasyRSA-3.0.4.tgz'
EASYRSA_DIR='EasyRSA-3.0.4'
EASYRSA_DEFAULT=path.abspath(path.join(path.dirname(__file__), 'tools', EASYRSA_DIR, 'easyrsa'))

def install_easyrsa():
    install_dir = path.abspath(path.join(path.dirname(__file__), 'tools'))

    if not path.isdir(install_dir):
        makedirs(install_dir)

    with requests.get(EASYRSA_URL, stream=True) as resp:
        tarball = tarfile.open(fileobj=io.BytesIO(resp.content), mode='r:gz')
        tarball.extractall(path=install_dir)

    logger.info("Installed easyrsa to '{}' from '{}'".format(install_dir, EASYRSA_URL))

def apply_defaults(config, defaults):
    # Expand the wildcard
    # Wildcard only makes sense when the value is a dict
    if wildcard in defaults:
        default = defaults[wildcard]
        defaults.update({k: default for k in config if k not in defaults})
        defaults.pop(wildcard)

    for key, default in defaults.items():
        # Handle the case where the key is not in config
        if key not in config:
            config[key] = default

        # Recurisly apply defaults to found dicts if the default is a dict
        elif isinstance(default, dict) and isinstance(config[key], dict):
            apply_defaults(config[key], default)

def read_config(filename):
    with open(filename, 'r') as config_file:
        config = yaml.load(config_file)

    logger.debug("Read from file: %s", config)

    apply_defaults(config, defaults)
    for chal_name, chal_settings in config['challenges'].items():
        if 'commonname' not in chal_settings:
            if config['domain']:
                chal_settings['commonname'] = '.'.join((chal_name, config['domain']))
            else:
                chal_settings['commonname'] = chal_name

    logger.debug("Modified: %s", config)

    return config

def parse_args():
    dir = path.dirname(__file__)

    parser = argparse.ArgumentParser(
            description='Parse the Naumachia config file and set up the environment',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--verbosity', '-v', metavar="LEVEL", default="info", choices=('critical', 'error', 'warning', 'info', 'debug'), help="logging level to use")
    parser.add_argument('--config', metavar="PATH", default=path.join(dir, 'config.yml'), help='path to Naumachia config file')
    parser.add_argument('--templates', metavar="PATH", default=path.join(dir, 'templates'), help='path to the configuration templates')
    parser.add_argument('--compose', metavar="PATH", default=path.join(dir, 'docker-compose.yml'), help='path to the rendered docker-compose output')
    parser.add_argument('--ovpn-configs', metavar="PATH", default=path.join(dir, 'openvpn', 'config'), help='path to openvpn configurations')
    parser.add_argument('--easyrsa', metavar="PATH", default=EASYRSA_DEFAULT, help='location of easyrsa executable. If the path does not exist, easyrsa will be installed')

    return parser.parse_args()

def init_pki(easyrsa, directory, cn):
    easyrsa = path.abspath(easyrsa)
    debug = logger.isEnabledFor(logging.DEBUG)
    common_args = {
        'check': True,
        'cwd': directory,
        'stdout': subprocess.PIPE if not debug else None,
        'stderr': subprocess.PIPE if not debug else None,
        'universal_newlines': True
    }

    try:
        logger.info("Initializing public key infrastructure (PKI)")
        subprocess.run([easyrsa, 'init-pki'], **common_args)
        logger.info("Building certificiate authority (CA)")
        subprocess.run([easyrsa, 'build-ca', 'nopass'], input="{}.{}\n".format('ca', cn), **common_args)
        logger.info("Generating Diffie-Hellman (DH) parameters")
        subprocess.run([easyrsa, 'gen-dh'], **common_args)
        logger.info("Building server certificiate")
        subprocess.run([easyrsa, 'build-server-full', cn, 'nopass'], **common_args)
        logger.info("Generating certificate revocation list (CRL)")
        subprocess.run([easyrsa, 'gen-crl'], **common_args)
    except subprocess.CalledProcessError as e:
        logger.error("Command '{}' failed with exit code {}".format(e.cmd, e.returncode))
        if e.output:
            logger.error(e.output)

def render(tpl_path, dst_path, context):
    dirname, filename = path.split(tpl_path)
    result = jinja2.Environment(
        loader=jinja2.FileSystemLoader(dirname or './')
    ).get_template(filename).render(context)

    with open(dst_path, 'w') as f:
        f.write(result)

    logger.info("Rendered {} from {} ".format(dst_path, tpl_path))

    return result


if __name__ == "__main__":
    args = parse_args()

    # Configure logging
    levelnum = getattr(logging, args.verbosity.upper(), None)
    if not isinstance(levelnum, int):
        raise ValueError('Invalid log level: {}'.format(args.verbosity))

    logging.basicConfig(level=levelnum, format="[%(levelname)s] %(message)s")

    # Load the config from disk
    logger.info("Using config from {}".format(args.config))
    config = read_config(args.config)

    # Ensure easyrsa is installed
    if not path.exists(args.easyrsa):
        if args.easyrsa == EASYRSA_DEFAULT:
            install_easyrsa()
        else:
            raise FileNotFoundError(args.easyrsa)

    # Render the docker-compose file
    template_path = path.join(args.templates, 'docker-compose.yml.j2')
    render(template_path, args.compose, config)

    # Create and missing openvpn config directories
    for name, chal in config['challenges'].items():
        config_dirname = path.join(args.ovpn_configs, name)
        logger.info("Configuring '{}'".format(name))

        if not path.isdir(config_dirname):
            makedirs(config_dirname)
            logger.info("Created new openvpn config directory {}".format(config_dirname))

            init_pki(args.easyrsa, config_dirname, chal['commonname'])
        else:
            logger.info("Using existing openvpn config directory {}".format(config_dirname))

        context = {'chal': chal}
        context.update(config)

        render(path.join(args.templates, 'ovpn_env.sh.j2'), path.join(config_dirname, 'ovpn_env.sh'), context)
        render(path.join(args.templates, 'openvpn.conf.j2'), path.join(config_dirname, 'openvpn.conf'), context)
