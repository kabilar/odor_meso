#! /usr/bin/env python

import os
import datajoint as dj


def getattrn(obj, *args):
    '''
    nested getattr(), or, getattr() N times.

    given some `foo.bar.baz`:

        >>> baz = getattrn(foo, 'bar', 'baz')
        >>> baz is foo.bar.baz
        True

    '''
    for a in args:
        obj = getattr(obj, a)  # will succeed or raise AttributeError.
    return obj


class PipelineCopy:

    # { modname : ((TableString, dup, extra, direct), ...), ... }
    copy_map = {
        'mice': (('Mice', False, False, False),),
        'shared': (('PipelineVersion', True, False, False),),
        'experiment': (
            ('Rig', True, False, False),
            ('Lens', True, False, False),
            ('Aim', True, False, False),
            ('Software', True, False, False),
            ('Person', None, None, None),  # in data_ingest?
            ('Anesthesia', None, None, None),  # in data_ingest?
            ('BrainArea', None, None, None),  # in data_ingest?
            ('Session', False, True, False),
            ('Scan', False, False, False),
            ('Scan.EyeVideo', False, False, False),
            ('Scan.BehaviorFile', False, False, False),
            ('Scan.Laser', False, False, False),
            ('TreadmillSpecs', False, False, False),),
        'odor': (
            ('Odorant', False, False, False),
            ('OdorSolution', False, False, False),
            ('OdorSession', False, False, False),
            ('OdorConfig', False, False, False),
            ('OdorRecording', False, False, False),
            ('MesoMatch', False, False, False),),
        'meso': (
            ('Version', False, False, False),
            ('ScanInfo', None, None, None),  # populate
            ('CorrectionChannel', False, False, False),
            ('SegmentationTask', False, False, False),
            ('Segmentation', False, False, False),
            ('Segmentation.Manual', False, False, True),
            ('Segmentation.Mask', False, False, True),)
    }

    def __init__(self, **args):

        src_args = ('SRC_USER', 'SRC_HOST', 'SRC_PASS')

        self.src_u, self.src_h, self.src_p = (
            args.get(v, None) for v in src_args)

        dst_args = ('DST_USER', 'DST_HOST', 'DST_PASS')

        self.dst_u, self.dst_h, self.dst_p = (
            args.get(v, None) for v in dst_args)

        try:
            assert(all((self.src_u, self.src_h, self.src_p)))
            assert(all((self.src_u, self.src_h, self.src_p)))
        except AssertionError:
            msg = 'configuration error: check {}'.format(
                (*src_args, *dst_args))
            raise RuntimeError(msg) from None

        self.src_c = dj.Connection(self.src_h, self.src_u, self.src_p)
        self.dst_c = dj.Connection(self.dst_h, self.dst_u, self.dst_p)

        src_vmod_cfg = {
            'mice': 'common_mice',
            'shared': 'pipeline_shared',
            'experiment': 'pipeilne_experiment',
            'odor': 'pipeline_odor',
            'meso': 'pipeline_meso',
            'eye': 'pipeline_eye',
        }

        dst_vmod_cfg = {
            'mice': 'test_mice',
            'shared': 'test_shared',
            'experiment': 'test_experiment',
            'odor': 'test_odor',
            'meso': 'test_meso',
            'eye': 'test_eye',
        }

        def make_vmods(tag, cfg, connection):
            return {k: dj.create_virtual_module(
                '{}_{}'.format(tag, k), v, connection=connection)
                    for k, v in cfg.items()}

        self.src_vmods = make_vmods('src', src_vmod_cfg, self.src_c)
        self.dst_vmods = make_vmods('dst', dst_vmod_cfg, self.dst_c)

    @classmethod
    def get(cls):
        return cls(**os.environ)

    def copy(self, restriction):

        for mod in PipelineCopy.copy_map:
            for spec in PipelineCopy.copy_map[mod]:
                tab, dups, extras, directs = spec

                if any((dups is None, extras is None, directs is None)):
                    print(f'skipping table {tab} - action unspecified')
                    continue

                self.copy1(mod, tab, {'skip_duplicates': dups,
                                      'ignore_extra_fields': extras,
                                      'allow_direct_insert': directs})

    def copy1(self, module_name, table_string, restriction, insert_args):

        tabs = table_string.split('.')

        src_m = self.src_vmods[module_name]
        src_t = getattrn(src_m, *tabs)

        dst_m = self.dst_vmods[module_name]
        dst_t = getattrn(dst_m, *tabs)

        try:
            dst_t.insert((src_t & restriction).fetch(), **insert_args)
        except Exception as e:
            print('error copying {}.{}: {}'.format(
                module_name, table_string, repr(e)))
            raise


if __name__ == '__main__':
    from code import interact
    interact('copy shell', local=locals())