# Development tool - sdk-update command plugin

import os
import subprocess
import logging
import glob
import shutil
import errno
import sys
import tempfile
from devtool import exec_build_env_command, setup_tinfoil, parse_recipe, DevtoolError

logger = logging.getLogger('devtool')

def parse_locked_sigs(sigfile_path):
    """Return <pn:task>:<hash> dictionary"""
    sig_dict = {}
    with open(sigfile_path) as f:
        lines = f.readlines()
        for line in lines:
            if ':' in line:
                taskkey, _, hashval = line.rpartition(':')
                sig_dict[taskkey.strip()] = hashval.split()[0]
    return sig_dict

def generate_update_dict(sigfile_new, sigfile_old):
    """Return a dict containing <pn:task>:<hash> which indicates what need to be updated"""
    update_dict = {}
    sigdict_new = parse_locked_sigs(sigfile_new)
    sigdict_old = parse_locked_sigs(sigfile_old)
    for k in sigdict_new:
        if k not in sigdict_old:
            update_dict[k] = sigdict_new[k]
            continue
        if sigdict_new[k] != sigdict_old[k]:
            update_dict[k] = sigdict_new[k]
            continue
    return update_dict

def get_sstate_objects(update_dict, sstate_dir):
    """Return a list containing sstate objects which are to be installed"""
    sstate_objects = []
    for k in update_dict:
        files = set()
        hashval = update_dict[k]
        p = sstate_dir + '/' + hashval[:2] + '/*' + hashval + '*.tgz'
        files |= set(glob.glob(p))
        p = sstate_dir + '/*/' + hashval[:2] + '/*' + hashval + '*.tgz'
        files |= set(glob.glob(p))
        files = list(files)
        if len(files) == 1:
            sstate_objects.extend(files)
        elif len(files) > 1:
            logger.error("More than one matching sstate object found for %s" % hashval)

    return sstate_objects

def mkdir(d):
    try:
        os.makedirs(d)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise e

def install_sstate_objects(sstate_objects, src_sdk, dest_sdk):
    """Install sstate objects into destination SDK"""
    sstate_dir = os.path.join(dest_sdk, 'sstate-cache')
    if not os.path.exists(sstate_dir):
        logger.error("Missing sstate-cache directory in %s, it might not be an extensible SDK." % dest_sdk)
        raise
    for sb in sstate_objects:
        dst = sb.replace(src_sdk, dest_sdk)
        destdir = os.path.dirname(dst)
        mkdir(destdir)
        logger.debug("Copying %s to %s" % (sb, dst))
        shutil.copy(sb, dst)

def check_manifest(fn, basepath):
    import bb.utils
    changedfiles = []
    with open(fn, 'r') as f:
        for line in f:
            splitline = line.split()
            if len(splitline) > 1:
                chksum = splitline[0]
                fpath = splitline[1]
                curr_chksum = bb.utils.sha256_file(os.path.join(basepath, fpath))
                if chksum != curr_chksum:
                    logger.debug('File %s changed: old csum = %s, new = %s' % (os.path.join(basepath, fpath), curr_chksum, chksum))
                    changedfiles.append(fpath)
    return changedfiles

def sdk_update(args, config, basepath, workspace):
    # Fetch locked-sigs.inc file from remote/local destination
    updateserver = args.updateserver
    if not updateserver:
        updateserver = config.get('SDK', 'updateserver', '')
    if not updateserver:
        raise DevtoolError("Update server not specified in config file, you must specify it on the command line")
    logger.debug("updateserver: %s" % updateserver)

    # Make sure we are using sdk-update from within SDK
    logger.debug("basepath = %s" % basepath)
    old_locked_sig_file_path = os.path.join(basepath, 'conf/locked-sigs.inc')
    if not os.path.exists(old_locked_sig_file_path):
        logger.error("Not using devtool's sdk-update command from within an extensible SDK. Please specify correct basepath via --basepath option")
        return -1
    else:
        logger.debug("Found conf/locked-sigs.inc in %s" % basepath)

    if ':' in updateserver:
        is_remote = True
    else:
        is_remote = False

    layers_dir = os.path.join(basepath, 'layers')
    conf_dir = os.path.join(basepath, 'conf')

    # Grab variable values
    tinfoil = setup_tinfoil(config_only=True, basepath=basepath)
    try:
        stamps_dir = tinfoil.config_data.getVar('STAMPS_DIR', True)
        sstate_mirrors = tinfoil.config_data.getVar('SSTATE_MIRRORS', True)
        site_conf_version = tinfoil.config_data.getVar('SITE_CONF_VERSION', True)
    finally:
        tinfoil.shutdown()

    if not is_remote:
        # devtool sdk-update /local/path/to/latest/sdk
        new_locked_sig_file_path = os.path.join(updateserver, 'conf/locked-sigs.inc')
        if not os.path.exists(new_locked_sig_file_path):
            logger.error("%s doesn't exist or is not an extensible SDK" % updateserver)
            return -1
        else:
            logger.debug("Found conf/locked-sigs.inc in %s" % updateserver)
        update_dict = generate_update_dict(new_locked_sig_file_path, old_locked_sig_file_path)
        logger.debug("update_dict = %s" % update_dict)
        sstate_dir = os.path.join(newsdk_path, 'sstate-cache')
        if not os.path.exists(sstate_dir):
            logger.error("sstate-cache directory not found under %s" % newsdk_path)
            return 1
        sstate_objects = get_sstate_objects(update_dict, sstate_dir)
        logger.debug("sstate_objects = %s" % sstate_objects)
        if len(sstate_objects) == 0:
            logger.info("No need to update.")
            return 0
        logger.info("Installing sstate objects into %s", basepath)
        install_sstate_objects(sstate_objects, updateserver.rstrip('/'), basepath)
        logger.info("Updating configuration files")
        new_conf_dir = os.path.join(updateserver, 'conf')
        shutil.rmtree(conf_dir)
        shutil.copytree(new_conf_dir, conf_dir)
        logger.info("Updating layers")
        new_layers_dir = os.path.join(updateserver, 'layers')
        shutil.rmtree(layers_dir)
        ret = subprocess.call("cp -a %s %s" % (new_layers_dir, layers_dir), shell=True)
        if ret != 0:
            logger.error("Copying %s to %s failed" % (new_layers_dir, layers_dir))
            return ret
    else:
        # devtool sdk-update http://myhost/sdk
        tmpsdk_dir = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmpsdk_dir, 'conf'))
            new_locked_sig_file_path = os.path.join(tmpsdk_dir, 'conf', 'locked-sigs.inc')
            # Fetch manifest from server
            tmpmanifest = os.path.join(tmpsdk_dir, 'conf', 'sdk-conf-manifest')
            ret = subprocess.call("wget -q -O %s %s/conf/sdk-conf-manifest" % (tmpmanifest, updateserver), shell=True)
            changedfiles = check_manifest(tmpmanifest, basepath)
            if not changedfiles:
                logger.info("Already up-to-date")
                return 0
            # Update metadata
            logger.debug("Updating metadata via git ...")
            # Try using 'git pull', if failed, use 'git clone'
            if os.path.exists(os.path.join(basepath, 'layers/.git')):
                ret = subprocess.call("git pull %s/layers/.git" % updateserver, shell=True, cwd=layers_dir)
            else:
                ret = -1
            if ret != 0:
                ret = subprocess.call("git clone %s/layers/.git" % updateserver, shell=True, cwd=tmpsdk_dir)
                if ret != 0:
                    logger.error("Updating metadata via git failed")
                    return ret
            logger.debug("Updating conf files ...")
            for changedfile in changedfiles:
                ret = subprocess.call("wget -q -O %s %s/%s" % (changedfile, updateserver, changedfile), shell=True, cwd=tmpsdk_dir)
                if ret != 0:
                    logger.error("Updating %s failed" % changedfile)
                    return ret

            # Ok, all is well at this point - move everything over
            tmplayers_dir = os.path.join(tmpsdk_dir, 'layers')
            if os.path.exists(tmplayers_dir):
                shutil.rmtree(layers_dir)
                shutil.move(tmplayers_dir, layers_dir)
            for changedfile in changedfiles:
                destfile = os.path.join(basepath, changedfile)
                os.remove(destfile)
                shutil.move(os.path.join(tmpsdk_dir, changedfile), destfile)
            os.remove(os.path.join(conf_dir, 'sdk-conf-manifest'))
            shutil.move(tmpmanifest, conf_dir)

            if not sstate_mirrors:
                with open(os.path.join(conf_dir, 'site.conf'), 'a') as f:
                    f.write('SCONF_VERSION = "%s"\n' % site_conf_version)
                    f.write('SSTATE_MIRRORS_append = " file://.* %s/sstate-cache/PATH \\n "\n' % updateserver)
        finally:
            shutil.rmtree(tmpsdk_dir)

    if not args.skip_prepare:
        # Find all potentially updateable tasks
        sdk_update_targets = []
        tasks = ['do_populate_sysroot', 'do_packagedata']
        for root, _, files in os.walk(stamps_dir):
            for fn in files:
                if not '.sigdata.' in fn:
                    for task in tasks:
                        if '.%s.' % task in fn or '.%s_setscene.' % task in fn:
                            sdk_update_targets.append('%s:%s' % (os.path.basename(root), task))
        # Run bitbake command for the whole SDK
        logger.info("Preparing build system... (This may take some time.)")
        try:
            exec_build_env_command(config.init_path, basepath, 'bitbake --setscene-only %s' % ' '.join(sdk_update_targets), stderr=subprocess.STDOUT)
            output, _ = exec_build_env_command(config.init_path, basepath, 'bitbake -n %s' % ' '.join(sdk_update_targets), stderr=subprocess.STDOUT)
            runlines = []
            for line in output.splitlines():
                if 'Running task ' in line:
                    runlines.append(line)
            if runlines:
                logger.error('Unexecuted tasks found in preparation log:\n  %s' % '\n  '.join(runlines))
                return -1
        except bb.process.ExecutionError as e:
            logger.error('Preparation failed:\n%s' % e.stdout)
            return -1
    return 0

def sdk_install(args, config, basepath, workspace):
    """Entry point for the devtool sdk-install command"""

    import oe.recipeutils
    import bb.process

    for recipe in args.recipename:
        if recipe in workspace:
            raise DevtoolError('recipe %s is a recipe in your workspace' % recipe)

    tasks = ['do_populate_sysroot', 'do_packagedata']
    stampprefixes = {}
    def checkstamp(recipe):
        stampprefix = stampprefixes[recipe]
        stamps = glob.glob(stampprefix + '*')
        for stamp in stamps:
            if '.sigdata.' not in stamp and stamp.startswith((stampprefix + '.', stampprefix + '_setscene.')):
                return True
        else:
            return False

    install_recipes = []
    tinfoil = setup_tinfoil(config_only=False, basepath=basepath)
    try:
        for recipe in args.recipename:
            rd = parse_recipe(config, tinfoil, recipe, True)
            if not rd:
                return 1
            stampprefixes[recipe] = '%s.%s' % (rd.getVar('STAMP', True), tasks[0])
            if checkstamp(recipe):
                logger.info('%s is already installed' % recipe)
            else:
                install_recipes.append(recipe)
    finally:
        tinfoil.shutdown()

    if install_recipes:
        logger.info('Installing %s...' % ', '.join(install_recipes))
        install_tasks = []
        for recipe in install_recipes:
            for task in tasks:
                if recipe.endswith('-native') and 'package' in task:
                    continue
                install_tasks.append('%s:%s' % (recipe, task))
        try:
            exec_build_env_command(config.init_path, basepath, 'bitbake --setscene-only %s' % ' '.join(install_tasks))
        except bb.process.ExecutionError as e:
            raise DevtoolError('Failed to install %s:\n%s' % (recipe, str(e)))
        failed = False
        for recipe in install_recipes:
            if checkstamp(recipe):
                logger.info('Successfully installed %s' % recipe)
            else:
                raise DevtoolError('Failed to install %s - unavailable' % recipe)
                failed = True
        if failed:
            return 2

def register_commands(subparsers, context):
    """Register devtool subcommands from the sdk plugin"""
    if context.fixed_setup:
        parser_sdk = subparsers.add_parser('sdk-update', help='Update SDK components from a nominated location')
        parser_sdk.add_argument('updateserver', help='The update server to fetch latest SDK components from', nargs='?')
        parser_sdk.add_argument('--skip-prepare', action="store_true", help='Skip re-preparing the build system after updating (for debugging only)')
        parser_sdk.set_defaults(func=sdk_update)
        parser_sdk_install = subparsers.add_parser('sdk-install', help='Install additional SDK components', description='Installs additional recipe development files into the SDK. (You can use "devtool search" to find available recipes.)')
        parser_sdk_install.add_argument('recipename', help='Name of the recipe to install the development artifacts for', nargs='+')
        parser_sdk_install.set_defaults(func=sdk_install)
