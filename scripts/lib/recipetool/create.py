# Recipe creation tool - create command plugin
#
# Copyright (C) 2014-2015 Intel Corporation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import sys
import os
import argparse
import glob
import fnmatch
import re
import json
import logging
import scriptutils
import urlparse

logger = logging.getLogger('recipetool')

tinfoil = None
plugins = None

def plugin_init(pluginlist):
    # Take a reference to the list so we can use it later
    global plugins
    plugins = pluginlist

def tinfoil_init(instance):
    global tinfoil
    tinfoil = instance

class RecipeHandler(object):
    recipelibmap = {}
    recipeheadermap = {}

    @staticmethod
    def load_libmap(d):
        '''Load library->recipe mapping'''
        import oe.package

        if RecipeHandler.recipelibmap:
            return
        # First build up library->package mapping
        shlib_providers = oe.package.read_shlib_providers(d)
        libdir = d.getVar('libdir', True)
        base_libdir = d.getVar('base_libdir', True)
        libpaths = list(set([base_libdir, libdir]))
        libname_re = re.compile('^lib(.+)\.so.*$')
        pkglibmap = {}
        for lib, item in shlib_providers.iteritems():
            for path, pkg in item.iteritems():
                if path in libpaths:
                    res = libname_re.match(lib)
                    if res:
                        libname = res.group(1)
                        if not libname in pkglibmap:
                            pkglibmap[libname] = pkg[0]
                    else:
                        logger.debug('unable to extract library name from %s' % lib)

        # Now turn it into a library->recipe mapping
        pkgdata_dir = d.getVar('PKGDATA_DIR', True)
        for libname, pkg in pkglibmap.iteritems():
            try:
                with open(os.path.join(pkgdata_dir, 'runtime', pkg)) as f:
                    for line in f:
                        if line.startswith('PN:'):
                            RecipeHandler.recipelibmap[libname] = line.split(':', 1)[-1].strip()
                            break
            except IOError as ioe:
                if ioe.errno == 2:
                    logger.warn('unable to find a pkgdata file for package %s' % pkg)
                else:
                    raise

        # Some overrides - these should be mapped to the virtual
        RecipeHandler.recipelibmap['GL'] = 'virtual/libgl'
        RecipeHandler.recipelibmap['EGL'] = 'virtual/egl'
        RecipeHandler.recipelibmap['GLESv2'] = 'virtual/libgles2'

    @staticmethod
    def load_headermap(d):
        '''Build up lib headerfile->recipe mapping'''
        if RecipeHandler.recipeheadermap:
            return
        includedir = d.getVar('includedir', True)
        for pkg in glob.glob(os.path.join(pkgdata_dir, 'runtime', '*-dev')):
            with open(os.path.join(pkgdata_dir, 'runtime', pkg)) as f:
                pn = None
                headers = []
                for line in f:
                    if line.startswith('PN:'):
                        pn = line.split(':', 1)[-1].strip()
                    elif line.startswith('FILES_INFO:'):
                        val = line.split(':', 1)[1].strip()
                        dictval = json.loads(val)
                        for fullpth in sorted(dictval):
                            if fullpth.startswith(includedir) and fullpth.endswith('.h'):
                                headers.append(os.path.relpath(fullpth, includedir))
                if pn and headers:
                    for header in headers:
                        RecipeHandler.recipeheadermap[header] = pn

    @staticmethod
    def checkfiles(path, speclist, recursive=False):
        results = []
        if recursive:
            for root, _, files in os.walk(path):
                for fn in files:
                    for spec in speclist:
                        if fnmatch.fnmatch(fn, spec):
                            results.append(os.path.join(root, fn))
        else:
            for spec in speclist:
                results.extend(glob.glob(os.path.join(path, spec)))
        return results

    @staticmethod
    def handle_depends(libdeps, pcdeps, deps, outlines, values, d):
        if pcdeps:
            recipemap = read_pkgconfig_provides(d)
        if libdeps:
            RecipeHandler.load_libmap(d)

        ignorelibs = ['socket']
        ignoredeps = ['gcc-runtime', 'glibc', 'uclibc', 'musl', 'tar-native', 'binutils-native']

        unmappedpc = []
        pcdeps = list(set(pcdeps))
        for pcdep in pcdeps:
            if isinstance(pcdep, basestring):
                recipe = recipemap.get(pcdep, None)
                if recipe:
                    deps.append(recipe)
                else:
                    if not pcdep.startswith('$'):
                        unmappedpc.append(pcdep)
            else:
                for item in pcdep:
                    recipe = recipemap.get(pcdep, None)
                    if recipe:
                        deps.append(recipe)
                        break
                else:
                    unmappedpc.append('(%s)' % ' or '.join(pcdep))

        unmappedlibs = []
        for libdep in libdeps:
            if isinstance(libdep, tuple):
                lib, header = libdep
            else:
                lib = libdep
                header = None

            if lib in ignorelibs:
                logger.debug('Ignoring library dependency %s' % lib)
                continue

            recipe = RecipeHandler.recipelibmap.get(lib, None)
            if recipe:
                deps.append(recipe)
            elif recipe is None:
                if header:
                    RecipeHandler.load_headermap(d)
                    recipe = RecipeHandler.recipeheadermap.get(header, None)
                    if recipe:
                        deps.append(recipe)
                    elif recipe is None:
                        unmappedlibs.append(lib)
                else:
                    unmappedlibs.append(lib)

        deps = set(deps).difference(set(ignoredeps))

        if unmappedpc:
            outlines.append('# NOTE: unable to map the following pkg-config dependencies: %s' % ' '.join(unmappedpc))
            outlines.append('#       (this is based on recipes that have previously been built and packaged)')

        if unmappedlibs:
            outlines.append('# NOTE: the following library dependencies are unknown, ignoring: %s' % ' '.join(list(set(unmappedlibs))))
            outlines.append('#       (this is based on recipes that have previously been built and packaged)')

        if deps:
            values['DEPENDS'] = ' '.join(deps)

    def genfunction(self, outlines, funcname, content, python=False, forcespace=False):
        if python:
            prefix = 'python '
        else:
            prefix = ''
        outlines.append('%s%s () {' % (prefix, funcname))
        if python or forcespace:
            indent = '    '
        else:
            indent = '\t'
        addnoop = not python
        for line in content:
            outlines.append('%s%s' % (indent, line))
            if addnoop:
                strippedline = line.lstrip()
                if strippedline and not strippedline.startswith('#'):
                    addnoop = False
        if addnoop:
            # Without this there'll be a syntax error
            outlines.append('%s:' % indent)
        outlines.append('}')
        outlines.append('')

    def process(self, srctree, classes, lines_before, lines_after, handled, extravalues):
        return False


def validate_pv(pv):
    if not pv or '_version' in pv.lower() or pv[0] not in '0123456789':
        return False
    return True

def determine_from_filename(srcfile):
    """Determine name and version from a filename"""
    part = ''
    if '.tar.' in srcfile:
        namepart = srcfile.split('.tar.')[0].lower()
    else:
        namepart = os.path.splitext(srcfile)[0].lower()
    splitval = namepart.rsplit('_', 1)
    if len(splitval) == 1:
        splitval = namepart.rsplit('-', 1)
    pn = splitval[0].replace('_', '-')
    if len(splitval) > 1:
        if splitval[1][0] in '0123456789':
            pv = splitval[1]
        else:
            pn = '-'.join(splitval).replace('_', '-')
            pv = None
    else:
        pv = None
    return (pn, pv)

def supports_srcrev(uri):
    localdata = bb.data.createCopy(tinfoil.config_data)
    # This is a bit sad, but if you don't have this set there can be some
    # odd interactions with the urldata cache which lead to errors
    localdata.setVar('SRCREV', '${AUTOREV}')
    bb.data.update_data(localdata)
    fetcher = bb.fetch2.Fetch([uri], localdata)
    urldata = fetcher.ud
    for u in urldata:
        if urldata[u].method.supports_srcrev():
            return True
    return False

def reformat_git_uri(uri):
    '''Convert any http[s]://....git URI into git://...;protocol=http[s]'''
    res = re.match('(https?)://([^;]+\.git)(;.*)?$', uri)
    if res:
        # Need to switch the URI around so that the git fetcher is used
        return 'git://%s;protocol=%s%s' % (res.group(2), res.group(1), res.group(3) or '')
    return uri

def create_recipe(args):
    import bb.process
    import tempfile
    import shutil

    pkgarch = ""
    if args.machine:
        pkgarch = "${MACHINE_ARCH}"

    checksums = (None, None)
    tempsrc = ''
    srcsubdir = ''
    srcrev = '${AUTOREV}'
    if '://' in args.source:
        # Fetch a URL
        fetchuri = reformat_git_uri(urlparse.urldefrag(args.source)[0])
        if args.binary:
            # Assume the archive contains the directory structure verbatim
            # so we need to extract to a subdirectory
            fetchuri += ';subdir=%s' % os.path.splitext(os.path.basename(urlparse.urlsplit(fetchuri).path))[0]
        srcuri = fetchuri
        rev_re = re.compile(';rev=([^;]+)')
        res = rev_re.search(srcuri)
        if res:
            srcrev = res.group(1)
            srcuri = rev_re.sub('', srcuri)
        tempsrc = tempfile.mkdtemp(prefix='recipetool-')
        srctree = tempsrc
        logger.info('Fetching %s...' % srcuri)
        try:
            checksums = scriptutils.fetch_uri(tinfoil.config_data, fetchuri, srctree, srcrev)
        except bb.fetch2.BBFetchException as e:
            logger.error(str(e).rstrip())
            sys.exit(1)
        dirlist = os.listdir(srctree)
        if 'git.indirectionsymlink' in dirlist:
            dirlist.remove('git.indirectionsymlink')
        if len(dirlist) == 1:
            singleitem = os.path.join(srctree, dirlist[0])
            if os.path.isdir(singleitem):
                # We unpacked a single directory, so we should use that
                srcsubdir = dirlist[0]
                srctree = os.path.join(srctree, srcsubdir)
            else:
                with open(singleitem, 'r') as f:
                    if '<html' in f.read(100).lower():
                        logger.error('Fetching "%s" returned a single HTML page - check the URL is correct and functional' % fetchuri)
                        sys.exit(1)
    else:
        # Assume we're pointing to an existing source tree
        if args.extract_to:
            logger.error('--extract-to cannot be specified if source is a directory')
            sys.exit(1)
        if not os.path.isdir(args.source):
            logger.error('Invalid source directory %s' % args.source)
            sys.exit(1)
        srctree = args.source
        srcuri = ''
        if os.path.exists(os.path.join(srctree, '.git')):
            # Try to get upstream repo location from origin remote
            try:
                stdout, _ = bb.process.run('git remote -v', cwd=srctree, shell=True)
            except bb.process.ExecutionError as e:
                stdout = None
            if stdout:
                for line in stdout.splitlines():
                    splitline = line.split()
                    if len(splitline) > 1:
                        if splitline[0] == 'origin' and '://' in splitline[1]:
                            srcuri = reformat_git_uri(splitline[1])
                            break

    if args.src_subdir:
        srcsubdir = os.path.join(srcsubdir, args.src_subdir)
        srctree_use = os.path.join(srctree, args.src_subdir)
    else:
        srctree_use = srctree

    if args.outfile and os.path.isdir(args.outfile):
        outfile = None
        outdir = args.outfile
    else:
        outfile = args.outfile
        outdir = None
    if outfile and outfile != '-':
        if os.path.exists(outfile):
            logger.error('Output file %s already exists' % outfile)
            sys.exit(1)

    lines_before = []
    lines_after = []

    lines_before.append('# Recipe created by %s' % os.path.basename(sys.argv[0]))
    lines_before.append('# This is the basis of a recipe and may need further editing in order to be fully functional.')
    lines_before.append('# (Feel free to remove these comments when editing.)')
    lines_before.append('#')

    licvalues = guess_license(srctree_use)
    lic_files_chksum = []
    if licvalues:
        licenses = []
        for licvalue in licvalues:
            if not licvalue[0] in licenses:
                licenses.append(licvalue[0])
            lic_files_chksum.append('file://%s;md5=%s' % (licvalue[1], licvalue[2]))
        lines_before.append('# WARNING: the following LICENSE and LIC_FILES_CHKSUM values are best guesses - it is')
        lines_before.append('# your responsibility to verify that the values are complete and correct.')
        if len(licvalues) > 1:
            lines_before.append('#')
            lines_before.append('# NOTE: multiple licenses have been detected; if that is correct you should separate')
            lines_before.append('# these in the LICENSE value using & if the multiple licenses all apply, or | if there')
            lines_before.append('# is a choice between the multiple licenses. If in doubt, check the accompanying')
            lines_before.append('# documentation to determine which situation is applicable.')
    else:
        lines_before.append('# Unable to find any files that looked like license statements. Check the accompanying')
        lines_before.append('# documentation and source headers and set LICENSE and LIC_FILES_CHKSUM accordingly.')
        lines_before.append('#')
        lines_before.append('# NOTE: LICENSE is being set to "CLOSED" to allow you to at least start building - if')
        lines_before.append('# this is not accurate with respect to the licensing of the software being built (it')
        lines_before.append('# will not be in most cases) you must specify the correct value before using this')
        lines_before.append('# recipe for anything other than initial testing/development!')
        licenses = ['CLOSED']
    lines_before.append('LICENSE = "%s"' % ' '.join(licenses))
    lines_before.append('LIC_FILES_CHKSUM = "%s"' % ' \\\n                    '.join(lic_files_chksum))
    lines_before.append('')

    classes = []

    # FIXME This is kind of a hack, we probably ought to be using bitbake to do this
    pn = None
    pv = None
    if outfile:
        recipefn = os.path.splitext(os.path.basename(outfile))[0]
        fnsplit = recipefn.split('_')
        if len(fnsplit) > 1:
            pn = fnsplit[0]
            pv = fnsplit[1]
        else:
            pn = recipefn

    if args.version:
        pv = args.version

    if args.name:
        pn = args.name
        if args.name.endswith('-native'):
            if args.also_native:
                logger.error('--also-native cannot be specified for a recipe named *-native (*-native denotes a recipe that is already only for native) - either remove the -native suffix from the name or drop --also-native')
                sys.exit(1)
            classes.append('native')
        elif args.name.startswith('nativesdk-'):
            if args.also_native:
                logger.error('--also-native cannot be specified for a recipe named nativesdk-* (nativesdk-* denotes a recipe that is already only for nativesdk)')
                sys.exit(1)
            classes.append('nativesdk')

    if pv and pv not in 'git svn hg'.split():
        realpv = pv
    else:
        realpv = None

    if srcuri and not realpv or not pn:
        parseres = urlparse.urlparse(srcuri)
        if parseres.path:
            srcfile = os.path.basename(parseres.path.rstrip('/'))
            name_pn, name_pv = determine_from_filename(srcfile)
            logger.debug('Determined from filename: name = "%s", version = "%s"' % (name_pn, name_pv))
            if name_pn and not pn:
                pn = name_pn
            if name_pv and not realpv:
                realpv = name_pv

    if not srcuri:
        lines_before.append('# No information for SRC_URI yet (only an external source tree was specified)')
    lines_before.append('SRC_URI = "%s"' % srcuri)
    (md5value, sha256value) = checksums
    if md5value:
        lines_before.append('SRC_URI[md5sum] = "%s"' % md5value)
    if sha256value:
        lines_before.append('SRC_URI[sha256sum] = "%s"' % sha256value)
    if srcuri and supports_srcrev(srcuri):
        lines_before.append('')
        lines_before.append('# Modify these as desired')
        lines_before.append('PV = "%s+git${SRCPV}"' % (realpv or '1.0'))
        lines_before.append('SRCREV = "%s"' % srcrev)
    lines_before.append('')

    if srcsubdir:
        lines_before.append('S = "${WORKDIR}/%s"' % srcsubdir)
        lines_before.append('')

    if pkgarch:
        lines_after.append('PACKAGE_ARCH = "%s"' % pkgarch)
        lines_after.append('')

    if args.binary:
        lines_after.append('INSANE_SKIP_${PN} += "already-stripped"')
        lines_after.append('')

    # Find all plugins that want to register handlers
    logger.debug('Loading recipe handlers')
    raw_handlers = []
    for plugin in plugins:
        if hasattr(plugin, 'register_recipe_handlers'):
            plugin.register_recipe_handlers(raw_handlers)
    # Sort handlers by priority
    handlers = []
    for i, handler in enumerate(raw_handlers):
        if isinstance(handler, tuple):
            handlers.append((handler[0], handler[1], i))
        else:
            handlers.append((handler, 0, i))
    handlers.sort(key=lambda item: (item[1], -item[2]), reverse=True)
    for handler, priority, _ in handlers:
        logger.debug('Handler: %s (priority %d)' % (handler.__class__.__name__, priority))
    handlers = [item[0] for item in handlers]

    # Apply the handlers
    handled = []

    if args.binary:
        classes.append('bin_package')
        handled.append('buildsystem')

    extravalues = {}
    for handler in handlers:
        handler.process(srctree_use, classes, lines_before, lines_after, handled, extravalues)

    if not realpv:
        realpv = extravalues.get('PV', None)
        if realpv:
            if not validate_pv(realpv):
                realpv = None
            else:
                realpv = realpv.lower().split()[0]
                if '_' in realpv:
                    realpv = realpv.replace('_', '-')
    if not pn:
        pn = extravalues.get('PN', None)
        if pn:
            if pn.startswith('GNU '):
                pn = pn[4:]
            if ' ' in pn:
                # Probably a descriptive identifier rather than a proper name
                pn = None
            else:
                pn = pn.lower()
                if '_' in pn:
                    pn = pn.replace('_', '-')

    if not outfile:
        if not pn:
            logger.error('Unable to determine short program name from source tree - please specify name with -N/--name or output file name with -o/--outfile')
            # devtool looks for this specific exit code, so don't change it
            sys.exit(15)
        else:
            if srcuri and srcuri.startswith(('git://', 'hg://', 'svn://')):
                outfile = '%s_%s.bb' % (pn, srcuri.split(':', 1)[0])
            elif realpv:
                outfile = '%s_%s.bb' % (pn, realpv)
            else:
                outfile = '%s.bb' % pn
            if outdir:
                outfile = os.path.join(outdir, outfile)
            # We need to check this again
            if os.path.exists(outfile):
                logger.error('Output file %s already exists' % outfile)
                sys.exit(1)

    lines = lines_before
    lines_before = []
    skipblank = True
    for line in lines:
        if skipblank:
            skipblank = False
            if not line:
                continue
        if line.startswith('S = '):
            if realpv and pv not in 'git svn hg'.split():
                line = line.replace(realpv, '${PV}')
            if pn:
                line = line.replace(pn, '${BPN}')
            if line == 'S = "${WORKDIR}/${BPN}-${PV}"':
                skipblank = True
                continue
        elif line.startswith('SRC_URI = '):
            if realpv:
                line = line.replace(realpv, '${PV}')
        elif line.startswith('PV = '):
            if realpv:
                line = re.sub('"[^+]*\+', '"%s+' % realpv, line)
        lines_before.append(line)

    if args.also_native:
        lines = lines_after
        lines_after = []
        bbclassextend = None
        for line in lines:
            if line.startswith('BBCLASSEXTEND ='):
                splitval = line.split('"')
                if len(splitval) > 1:
                    bbclassextend = splitval[1].split()
                    if not 'native' in bbclassextend:
                        bbclassextend.insert(0, 'native')
                line = 'BBCLASSEXTEND = "%s"' % ' '.join(bbclassextend)
            lines_after.append(line)
        if not bbclassextend:
            lines_after.append('BBCLASSEXTEND = "native"')

    outlines = []
    outlines.extend(lines_before)
    if classes:
        if outlines[-1] and not outlines[-1].startswith('#'):
            outlines.append('')
        outlines.append('inherit %s' % ' '.join(classes))
        outlines.append('')
    outlines.extend(lines_after)

    if args.extract_to:
        scriptutils.git_convert_standalone_clone(srctree)
        if os.path.isdir(args.extract_to):
            # If the directory exists we'll move the temp dir into it instead of
            # its contents - of course, we could try to always move its contents
            # but that is a pain if there are symlinks; the simplest solution is
            # to just remove it first
            os.rmdir(args.extract_to)
        shutil.move(srctree, args.extract_to)
        if tempsrc == srctree:
            tempsrc = None
        logger.info('Source extracted to %s' % args.extract_to)

    if outfile == '-':
        sys.stdout.write('\n'.join(outlines) + '\n')
    else:
        with open(outfile, 'w') as f:
            f.write('\n'.join(outlines) + '\n')
        logger.info('Recipe %s has been created; further editing may be required to make it fully functional' % outfile)

    if tempsrc:
        shutil.rmtree(tempsrc)

    return 0

def get_license_md5sums(d, static_only=False):
    import bb.utils
    md5sums = {}
    if not static_only:
        # Gather md5sums of license files in common license dir
        commonlicdir = d.getVar('COMMON_LICENSE_DIR', True)
        for fn in os.listdir(commonlicdir):
            md5value = bb.utils.md5_file(os.path.join(commonlicdir, fn))
            md5sums[md5value] = fn
    # The following were extracted from common values in various recipes
    # (double checking the license against the license file itself, not just
    # the LICENSE value in the recipe)
    md5sums['94d55d512a9ba36caa9b7df079bae19f'] = 'GPLv2'
    md5sums['b234ee4d69f5fce4486a80fdaf4a4263'] = 'GPLv2'
    md5sums['59530bdf33659b29e73d4adb9f9f6552'] = 'GPLv2'
    md5sums['0636e73ff0215e8d672dc4c32c317bb3'] = 'GPLv2'
    md5sums['eb723b61539feef013de476e68b5c50a'] = 'GPLv2'
    md5sums['751419260aa954499f7abaabaa882bbe'] = 'GPLv2'
    md5sums['393a5ca445f6965873eca0259a17f833'] = 'GPLv2'
    md5sums['12f884d2ae1ff87c09e5b7ccc2c4ca7e'] = 'GPLv2'
    md5sums['8ca43cbc842c2336e835926c2166c28b'] = 'GPLv2'
    md5sums['ebb5c50ab7cab4baeffba14977030c07'] = 'GPLv2'
    md5sums['c93c0550bd3173f4504b2cbd8991e50b'] = 'GPLv2'
    md5sums['9ac2e7cff1ddaf48b6eab6028f23ef88'] = 'GPLv2'
    md5sums['4325afd396febcb659c36b49533135d4'] = 'GPLv2'
    md5sums['18810669f13b87348459e611d31ab760'] = 'GPLv2'
    md5sums['d7810fab7487fb0aad327b76f1be7cd7'] = 'GPLv2' # the Linux kernel's COPYING file
    md5sums['bbb461211a33b134d42ed5ee802b37ff'] = 'LGPLv2.1'
    md5sums['7fbc338309ac38fefcd64b04bb903e34'] = 'LGPLv2.1'
    md5sums['4fbd65380cdd255951079008b364516c'] = 'LGPLv2.1'
    md5sums['2d5025d4aa3495befef8f17206a5b0a1'] = 'LGPLv2.1'
    md5sums['fbc093901857fcd118f065f900982c24'] = 'LGPLv2.1'
    md5sums['a6f89e2100d9b6cdffcea4f398e37343'] = 'LGPLv2.1'
    md5sums['d8045f3b8f929c1cb29a1e3fd737b499'] = 'LGPLv2.1'
    md5sums['fad9b3332be894bab9bc501572864b29'] = 'LGPLv2.1'
    md5sums['3bf50002aefd002f49e7bb854063f7e7'] = 'LGPLv2'
    md5sums['9f604d8a4f8e74f4f5140845a21b6674'] = 'LGPLv2'
    md5sums['5f30f0716dfdd0d91eb439ebec522ec2'] = 'LGPLv2'
    md5sums['55ca817ccb7d5b5b66355690e9abc605'] = 'LGPLv2'
    md5sums['252890d9eee26aab7b432e8b8a616475'] = 'LGPLv2'
    md5sums['3214f080875748938ba060314b4f727d'] = 'LGPLv2'
    md5sums['db979804f025cf55aabec7129cb671ed'] = 'LGPLv2'
    md5sums['d32239bcb673463ab874e80d47fae504'] = 'GPLv3'
    md5sums['f27defe1e96c2e1ecd4e0c9be8967949'] = 'GPLv3'
    md5sums['6a6a8e020838b23406c81b19c1d46df6'] = 'LGPLv3'
    md5sums['3b83ef96387f14655fc854ddc3c6bd57'] = 'Apache-2.0'
    md5sums['385c55653886acac3821999a3ccd17b3'] = 'Artistic-1.0 | GPL-2.0' # some perl modules
    md5sums['54c7042be62e169199200bc6477f04d1'] = 'BSD-3-Clause'
    return md5sums

def guess_license(srctree):
    import bb
    md5sums = get_license_md5sums(tinfoil.config_data)

    licenses = []
    licspecs = ['LICENSE*', 'COPYING*', '*[Ll]icense*', 'LICENCE*', 'LEGAL*', '[Ll]egal*', '*GPL*', 'README.lic*', 'COPYRIGHT*', '[Cc]opyright*']
    licfiles = []
    for root, dirs, files in os.walk(srctree):
        for fn in files:
            for spec in licspecs:
                if fnmatch.fnmatch(fn, spec):
                    fullpath = os.path.join(root, fn)
                    if not fullpath in licfiles:
                        licfiles.append(fullpath)
    for licfile in licfiles:
        md5value = bb.utils.md5_file(licfile)
        license = md5sums.get(md5value, 'Unknown')
        licenses.append((license, os.path.relpath(licfile, srctree), md5value))

    # FIXME should we grab at least one source file with a license header and add that too?

    return licenses

def read_pkgconfig_provides(d):
    pkgdatadir = d.getVar('PKGDATA_DIR', True)
    pkgmap = {}
    for fn in glob.glob(os.path.join(pkgdatadir, 'shlibs2', '*.pclist')):
        with open(fn, 'r') as f:
            for line in f:
                pkgmap[os.path.basename(line.rstrip())] = os.path.splitext(os.path.basename(fn))[0]
    recipemap = {}
    for pc, pkg in pkgmap.iteritems():
        pkgdatafile = os.path.join(pkgdatadir, 'runtime', pkg)
        if os.path.exists(pkgdatafile):
            with open(pkgdatafile, 'r') as f:
                for line in f:
                    if line.startswith('PN: '):
                        recipemap[pc] = line.split(':', 1)[1].strip()
    return recipemap

def convert_pkginfo(pkginfofile):
    values = {}
    with open(pkginfofile, 'r') as f:
        indesc = False
        for line in f:
            if indesc:
                if line.strip():
                    values['DESCRIPTION'] += ' ' + line.strip()
                else:
                    indesc = False
            else:
                splitline = line.split(': ', 1)
                key = line[0]
                value = line[1]
                if key == 'LICENSE':
                    for dep in value.split(','):
                        dep = dep.split()[0]
                        mapped = depmap.get(dep, '')
                        if mapped:
                            depends.append(mapped)
                elif key == 'License':
                    values['LICENSE'] = value
                elif key == 'Summary':
                    values['SUMMARY'] = value
                elif key == 'Description':
                    values['DESCRIPTION'] = value
                    indesc = True
    return values

def convert_debian(debpath):
    # FIXME extend this mapping - perhaps use distro_alias.inc?
    depmap = {'libz-dev': 'zlib'}

    values = {}
    depends = []
    with open(os.path.join(debpath, 'control')) as f:
        indesc = False
        for line in f:
            if indesc:
                if line.strip():
                    if line.startswith(' This package contains'):
                        indesc = False
                    else:
                        values['DESCRIPTION'] += ' ' + line.strip()
                else:
                    indesc = False
            else:
                splitline = line.split(':', 1)
                key = line[0]
                value = line[1]
                if key == 'Build-Depends':
                    for dep in value.split(','):
                        dep = dep.split()[0]
                        mapped = depmap.get(dep, '')
                        if mapped:
                            depends.append(mapped)
                elif key == 'Section':
                    values['SECTION'] = value
                elif key == 'Description':
                    values['SUMMARY'] = value
                    indesc = True

    if depends:
        values['DEPENDS'] = ' '.join(depends)

    return values


def register_commands(subparsers):
    parser_create = subparsers.add_parser('create',
                                          help='Create a new recipe',
                                          description='Creates a new recipe from a source tree')
    parser_create.add_argument('source', help='Path or URL to source')
    parser_create.add_argument('-o', '--outfile', help='Specify filename for recipe to create')
    parser_create.add_argument('-m', '--machine', help='Make recipe machine-specific as opposed to architecture-specific', action='store_true')
    parser_create.add_argument('-x', '--extract-to', metavar='EXTRACTPATH', help='Assuming source is a URL, fetch it and extract it to the directory specified as %(metavar)s')
    parser_create.add_argument('-N', '--name', help='Name to use within recipe (PN)')
    parser_create.add_argument('-V', '--version', help='Version to use within recipe (PV)')
    parser_create.add_argument('-b', '--binary', help='Treat the source tree as something that should be installed verbatim (no compilation, same directory structure)', action='store_true')
    parser_create.add_argument('--also-native', help='Also add native variant (i.e. support building recipe for the build host as well as the target machine)', action='store_true')
    parser_create.add_argument('--src-subdir', help='Specify subdirectory within source tree to use', metavar='SUBDIR')
    parser_create.set_defaults(func=create_recipe)

