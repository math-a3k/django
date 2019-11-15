import codecs
import concurrent.futures
import glob
import os
from subprocess import DEVNULL, PIPE

from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import (
    find_command, is_ignored_path, popen_wrapper,
)


def has_bom(fn):
    with open(fn, 'rb') as f:
        sample = f.read(4)
    return sample.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))


def is_writable(path):
    # Known side effect: updating file access/modified time to current time if
    # it is writable.
    try:
        with open(path, 'a'):
            os.utime(path, None)
    except OSError:
        return False
    return True


class Command(BaseCommand):
    help = 'Compiles .po files to .mo files for use with builtin gettext support.'

    requires_system_checks = False

    program = 'msgfmt'
    program_options = ['--check-format']
    dry_run = False

    def add_arguments(self, parser):
        parser.add_argument(
            '--locale', '-l', action='append', default=[],
            help='Locale(s) to process (e.g. de_AT). Default is to process all. '
                 'Can be used multiple times.',
        )
        parser.add_argument(
            '--exclude', '-x', action='append', default=[],
            help='Locales to exclude. Default is none. Can be used multiple times.',
        )
        parser.add_argument(
            '--use-fuzzy', '-f', dest='fuzzy', action='store_true',
            help='Use fuzzy translations.',
        )
        parser.add_argument(
            '--ignore', '-i', action='append', dest='ignore_patterns',
            default=[], metavar='PATTERN',
            help='Ignore directories matching this glob-style pattern. '
                 'Use multiple times to ignore more.',
        )
        parser.add_argument(
            '--main-po', '-m', dest='main-po', action='store_true',
            help='Compile main .po file.'
        )
        parser.add_argument(
            '-n', '--dry-run', action='store_true', dest='dry_run',
            help="Do everything except modify the filesystem.",
        )

    def handle(self, **options):
        locale = options['locale']
        exclude = options['exclude']
        ignore_patterns = set(options['ignore_patterns'])
        compile_main_po = options['main-po']
        self.verbosity = options['verbosity']
        self.dry_run = options['dry_run']
        if options['fuzzy']:
            self.program_options = self.program_options + ['-f']

        if find_command(self.program) is None:
            raise CommandError("Can't find %s. Make sure you have GNU gettext "
                               "tools 0.15 or newer installed." % self.program)

        if self.dry_run:
            self.stdout.write(
                'You have activated the --dry-run option so no files will be modified.\n\n'
            )

        basedirs = [os.path.join('conf', 'locale'), 'locale']
        if os.environ.get('DJANGO_SETTINGS_MODULE'):
            from django.conf import settings
            basedirs.extend(settings.LOCALE_PATHS)

        # Walk entire tree, looking for locale directories
        for dirpath, dirnames, filenames in os.walk('.', topdown=True):
            for dirname in dirnames:
                if is_ignored_path(os.path.normpath(os.path.join(dirpath, dirname)), ignore_patterns):
                    dirnames.remove(dirname)
                elif dirname == 'locale':
                    basedirs.append(os.path.join(dirpath, dirname))

        # Gather existing directories.
        basedirs = set(map(os.path.abspath, filter(os.path.isdir, basedirs)))

        # Address main po file only if it is run from a project directory,
        # not from the Django Git Checkout
        if compile_main_po and not os.path.join('conf', 'locale') in basedirs:
            import django
            django_dir = os.path.normpath(os.path.join(os.path.dirname(django.__file__)))
            basedirs.update([os.path.join(django_dir, 'conf', 'locale')])

        if not basedirs:
            raise CommandError("This script should be run from the Django Git "
                               "checkout or your project or app tree, or with "
                               "the -m option, or with the settings module "
                               "specified.")

        # Build locale list
        all_locales = []
        for basedir in basedirs:
            locale_dirs = filter(os.path.isdir, glob.glob('%s/*' % basedir))
            all_locales.extend(map(os.path.basename, locale_dirs))

        # Account for excluded locales
        locales = locale or all_locales
        locales = set(locales).difference(exclude)

        self.has_errors = False
        for basedir in basedirs:
            if locales:
                dirs = [os.path.join(basedir, l, 'LC_MESSAGES') for l in locales]
            else:
                dirs = [basedir]
            locations = []
            for ldir in dirs:
                for dirpath, dirnames, filenames in os.walk(ldir):
                    locations.extend((dirpath, f) for f in filenames if f.endswith('.po'))
            if locations:
                self.compile_messages(locations)

        if self.has_errors:
            raise CommandError('compilemessages generated one or more errors.')

    def compile_messages(self, locations):
        """
        Locations is a list of tuples: [(directory, file), ...]
        """
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = []
            for i, (dirpath, f) in enumerate(locations):
                if self.verbosity > 0:
                    self.stdout.write('processing file %s in %s\n' % (f, dirpath))
                po_path = os.path.join(dirpath, f)
                if has_bom(po_path):
                    self.stderr.write(
                        'The %s file has a BOM (Byte Order Mark). Django only '
                        'supports .po files encoded in UTF-8 and without any BOM.' % po_path
                    )
                    self.has_errors = True
                    continue
                base_path = os.path.splitext(po_path)[0]

                # Check writability on first location
                if i == 0 and not is_writable(base_path + '.mo'):
                    self.stderr.write(
                        'The po files under %s are in a seemingly not writable location. '
                        'mo files will not be updated/created.' % dirpath
                    )
                    self.has_errors = True
                    return

                if not self.dry_run:
                    output_file = base_path + '.mo'
                    stdout_handler = PIPE
                else:
                    output_file = '-'
                    stdout_handler = DEVNULL

                args = [self.program] + self.program_options + [
                    '-o', output_file, base_path + '.po',
                ]
                futures.append(executor.submit(popen_wrapper, args, stdout=stdout_handler))

            for future in concurrent.futures.as_completed(futures):
                output, errors, status = future.result()
                if status:
                    if self.verbosity > 0:
                        if errors:
                            self.stderr.write("Execution of %s failed: %s" % (self.program, errors))
                        else:
                            self.stderr.write("Execution of %s failed" % self.program)
                    self.has_errors = True
