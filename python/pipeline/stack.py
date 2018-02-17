""" Schemas for structural stacks. """
import datajoint as dj
from datajoint.jobs import key_hash
import matplotlib.pyplot as plt
import numpy as np
import scanreader
from scipy import signal
import itertools

from . import experiment, notify, shared, reso, meso
from .utils import galvo_corrections, stitching, performance
from .utils.signal import mirrconv, float2uint8
from .exceptions import PipelineException


schema = dj.schema('pipeline_stack', locals(), create_tables=True)
CURRENT_VERSION = 1


@schema
class Version(dj.Lookup):
    definition = """ # versions for the stack pipeline

    -> shared.PipelineVersion
    ---
    description = ''                : varchar(256)      # any notes on this version
    date = CURRENT_TIMESTAMP        : timestamp         # automatic
    """


@schema
class StackInfo(dj.Imported):
    definition = """ # master table with general data about the stacks

    -> experiment.Stack
    -> Version                          # stack version
    ---
    nrois           : tinyint           # number of ROIs
    nchannels       : tinyint           # number of channels
    z_step          : float             # (um) distance in z between adjacent slices (always positive)
    fill_fraction   : float             # raster scan temporal fill fraction (see scanimage)
    """
    @property
    def key_source(self):
        return experiment.Stack() * (Version() & {'pipe_version': CURRENT_VERSION})

    class ROI(dj.Part):
        definition = """ # 3-D volumes that compose this stack (usually tiled to form a bigger fov)

        -> StackInfo
        roi_id          : tinyint           # same as ScanImage's
        ---
        -> experiment.Stack.Filename
        field_ids           : blob              # list of field_ids (0-index) sorted from shallower to deeper
        roi_x               : float             # (um) center of ROI in the motor coordinate system
        roi_y               : float             # (um) center of ROI in the motor coordinate system
        roi_z               : float             # (um) initial depth in the motor coordinate system
        roi_px_height       : smallint          # lines per frame
        roi_px_width        : smallint          # pixels per line
        roi_px_depth        : smallint          # number of slices
        roi_um_height       : float             # height in microns
        roi_um_width        : float             # width in microns
        roi_um_depth        : float             # depth in microns
        nframes             : smallint          # number of recorded frames per plane
        fps                 : float             # (Hz) volumes per second
        bidirectional       : boolean           # true = bidirectional scanning
        is_slow             : boolean           # whether all frames in one depth were recorded before moving to the next
        """

        def _make_tuples(self, key, stack, id_in_file):
            # Create results tuple
            tuple_ = key.copy()

            # Get field_ids ordered from shallower to deeper field in this ROI
            surf_z = (experiment.Stack() & key).fetch1('surf_depth')  # surface depth in fastZ coordinates (meso) or motor coordinates (reso)
            if stack.is_multiROI:
                field_ids = [i for i, field_roi in enumerate(stack.field_rois) if id_in_file in field_roi]
                field_depths = [stack.field_depths[i] - surf_z for i in field_ids]
            else:
                field_ids = range(stack.num_scanning_depths)
                motor_zero = surf_z - stack.motor_position_at_zero[2]
                if stack.is_slow_stack and not stack.is_slow_stack_with_fastZ: # using motor
                    initial_fastZ = stack.initial_secondary_z or 0
                    field_depths = [motor_zero - stack.field_depths[i] + 2 * initial_fastZ for i in field_ids]
                else: # using fastZ
                    field_depths = [motor_zero + stack.field_depths[i] for i in field_ids]
            field_depths, field_ids = zip(*sorted(zip(field_depths, field_ids)))
            tuple_['field_ids'] = field_ids

            # Get reso/meso specific coordinates
            x_zero, y_zero, _ = stack.motor_position_at_zero  # motor x, y at ScanImage's 0
            if stack.is_multiROI:
                tuple_['roi_x'] = x_zero + stack._degrees_to_microns(stack.fields[field_ids[0]].x)
                tuple_['roi_y'] = y_zero + stack._degrees_to_microns(stack.fields[field_ids[0]].y)
                tuple_['roi_px_height'] = stack.field_heights[field_ids[0]]
                tuple_['roi_px_width'] = stack.field_widths[field_ids[0]]
                tuple_['roi_um_height'] = stack.field_heights_in_microns[field_ids[0]]
                tuple_['roi_um_width'] = stack.field_widths_in_microns[field_ids[0]]
            else:
                tuple_['roi_x'] = x_zero
                tuple_['roi_y'] = y_zero #TODO: Add sign flip if ys in reso point upwards
                tuple_['roi_px_height'] = stack.image_height
                tuple_['roi_px_width'] = stack.image_width

                # Estimate height and width in microns using measured FOVs for similar setups
                fov_rel = (experiment.FOV() * experiment.Session() * experiment.Stack() &
                           key & 'session_date>=fov_ts')
                zooms = fov_rel.fetch('mag').astype(np.float32)  # zooms measured in same setup
                closest_zoom = zooms[np.argmin(np.abs(np.log(zooms / stack.zoom)))]
                dims = (fov_rel & 'ABS(mag - {}) < 1e-4'.format(closest_zoom)).fetch1('height', 'width')
                um_height, um_width = [float(um) * (closest_zoom / stack.zoom) for um in dims]
                tuple_['roi_um_height'] = um_height * stack._y_angle_scale_factor
                tuple_['roi_um_width'] = um_width * stack._x_angle_scale_factor

            # Get common parameters
            tuple_['roi_z'] = field_depths[0] #TODO: Add surf_depth
            tuple_['roi_px_depth'] = len(field_ids)
            tuple_['roi_um_depth'] = field_depths[-1] - field_depths[0] + 1
            tuple_['nframes'] = stack.num_frames
            tuple_['fps'] = stack.fps
            tuple_['bidirectional'] = stack.is_bidirectional
            tuple_['is_slow'] = stack.is_slow_stack

            self.insert1(tuple_)

        @property
        def microns_per_pixel(self):
            """ Returns an array with microns per pixel in height and width. """
            dims = self.fetch1('roi_um_height', 'roi_px_height', 'roi_um_width', 'roi_px_width')
            um_height, px_height, um_width, px_width = dims
            return np.array([um_height / px_height, um_width / px_width])

    def _make_tuples(self, key):
        """ Read and store stack information."""
        print('Reading header...')

        # Read files forming this stack
        filename_keys = (experiment.Stack.Filename() & key).fetch(dj.key)
        stacks = []
        for filename_key in filename_keys:
            stack_filename = (experiment.Stack.Filename() & filename_key).local_filenames_as_wildcard
            stacks.append(scanreader.read_scan(stack_filename))
        num_rois_per_file = [(stack.num_rois if stack.is_multiROI else 1) for stack in stacks]

        # Create Stack tuple
        tuple_ = key.copy()
        tuple_['nrois'] = np.sum(num_rois_per_file)
        tuple_['nchannels'] = stacks[0].num_channels
        tuple_['z_step'] = abs(stacks[0].scanning_depths[1] - stacks[0].scanning_depths[0])
        tuple_['fill_fraction'] = stacks[0].temporal_fill_fraction

        # Insert Stack
        StackInfo().insert1(tuple_)

        # Insert ROIs
        roi_id = 1
        for filename_key, num_rois, stack in zip(filename_keys, num_rois_per_file, stacks):
            for roi_id_in_file in range(num_rois):
                roi_key = {**key, **filename_key, 'roi_id': roi_id}
                StackInfo.ROI()._make_tuples(roi_key, stack, roi_id_in_file)
                roi_id += 1

        # Fill in CorrectionChannel if only one channel
        if stacks[0].num_channels == 1:
            CorrectionChannel().fill_in(key)

        self.notify(key)

    @notify.ignore_exceptions
    def notify(self, key):
        msg = 'StackInfo for `{}` has been populated.'.format(key)
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg)


@schema
class Quality(dj.Computed):
    definition = """ # different quality metrics for a scan (before corrections)

    -> StackInfo
    """
    @property
    def key_source(self):
        return StackInfo() & {'pipe_version': CURRENT_VERSION}

    class MeanIntensity(dj.Part):
        definition = """ # mean intensity per frame and slice

        -> Quality
        -> StackInfo.ROI
        -> shared.Channel
        ---
        intensities                 : longblob      # num_slices x num_frames
        """

    class SummaryFrames(dj.Part):
        definition = """ # mean slice at 8 different depths

        -> Quality
        -> StackInfo.ROI
        -> shared.Channel
        ---
        summary                     : longblob      # h x w x 8
        """

    class Contrast(dj.Part):
        definition = """ # difference between 99 and 1 percentile per frame and slice

        -> Quality
        -> StackInfo.ROI
        -> shared.Channel
        ---
        contrasts                   : longblob      # num_slices x num_frames
        """

    def _make_tuples(self, key):
        print('Computing quality metrics for stack', key)

        # Insert in Quality
        self.insert1(key)

        for roi_tuple in (StackInfo.ROI() & key).fetch():
            # Load ROI
            roi_filename = (experiment.Stack.Filename() & roi_tuple).local_filenames_as_wildcard
            roi = scanreader.read_scan(roi_filename)

            for channel in range((StackInfo() & key).fetch1('nchannels')):
                # Map: Compute quality metrics in each field
                f = performance.parallel_quality_stack # function to map
                field_ids = roi_tuple['field_ids']
                results = performance.map_fields(f, roi, field_ids=field_ids, channel=channel)

                # Reduce: Collect results
                mean_intensities = np.empty((roi_tuple['roi_px_depth'], roi_tuple['nframes']))
                contrasts = np.empty((roi_tuple['roi_px_depth'], roi_tuple['nframes']))
                for field_idx, field_mis, field_contrasts, _ in results:
                    mean_intensities[field_idx] = field_mis
                    contrasts[field_idx] = field_contrasts
                frames = [res[3] for res in sorted(results, key=lambda res: res[0])]
                frames = np.stack(frames[:: int(len(frames) / 8)], axis=-1) # frames at 8 diff depths

                # Insert
                roi_key = {**key, 'roi_id': roi_tuple['roi_id'], 'channel': channel + 1}
                self.MeanIntensity().insert1({**roi_key, 'intensities': mean_intensities})
                self.Contrast().insert1({**roi_key, 'contrasts': contrasts})
                self.SummaryFrames().insert1({**roi_key, 'summary': frames})

                self.notify(roi_key, frames, mean_intensities, contrasts)

    @notify.ignore_exceptions
    def notify(self, key, summary_frames, mean_intensities, contrasts):
        """ Sends slack notification for a single slice + channel combination. """
        # Send summary frames
        import imageio
        video_filename = '/tmp/' + key_hash(key) + '.gif'
        percentile_99th = np.percentile(summary_frames, 99.5)
        summary_frames = np.clip(summary_frames, None, percentile_99th)
        summary_frames = float2uint8(summary_frames).transpose([2, 0, 1])
        imageio.mimsave(video_filename, summary_frames, duration=0.4)

        msg = 'Quality for `{}` has been populated.'.format(key)
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg, file=video_filename,
                                                                   file_title='summary frames')

        # Send intensity and contrasts
        figsize = (min(4, contrasts.shape[1] / 10 + 1),  contrasts.shape[0] / 30 + 1) # set heuristically
        fig, axes = plt.subplots(1, 2, figsize=figsize, sharex=True, sharey=True)
        fig.tight_layout()
        axes[0].set_title('Mean intensity', size='small')
        axes[0].imshow(mean_intensities)
        axes[0].set_ylabel('Slices')
        axes[0].set_xlabel('Frames')
        axes[1].set_title('Contrast (99 - 1 percentile)', size='small')
        axes[1].imshow(contrasts)
        axes[1].set_xlabel('Frames')
        img_filename = '/tmp/' + key_hash(key) + '.png'
        fig.savefig(img_filename, bbox_inches='tight')
        plt.close(fig)

        (notify.SlackUser() & (experiment.Session() & key)).notify(file=img_filename, file_title='quality images')


@schema
class CorrectionChannel(dj.Manual):
    definition = """ # channel to use for raster and motion correction

    -> experiment.Stack
    ---
    -> shared.Channel
    """

    def fill_in(self, key, channel=1):
        for stack_key in (StackInfo() & key).fetch(dj.key):
            self.insert1({**stack_key, 'channel': channel}, ignore_extra_fields=True,
                          skip_duplicates=True)


@schema
class RasterCorrection(dj.Computed):
    definition = """ # raster correction for bidirectional resonant scans

    -> StackInfo.ROI                         # animal_id, session, stack_idx, roi_id, version
    -> CorrectionChannel                     # animal_id, session, stack_idx
    ---
    raster_phase            : float          # difference between expected and recorded scan angle
    raster_std              : float          # standard deviation among raster phases in different slices
    """
    @property
    def key_source(self):
        return StackInfo.ROI() * CorrectionChannel() & {'pipe_version': CURRENT_VERSION}

    def _make_tuples(self, key):
        """ Compute raster phase discarding top and bottom 15% of slices and tapering
        edges to avoid edge artifacts."""
        print('Computing raster correction for ROI', key)

        # Get some params
        res = (StackInfo.ROI() & key).fetch1('bidirectional', 'roi_px_height',
                                             'roi_px_width', 'field_ids')
        is_bidirectional, image_height, image_width, field_ids = res
        correction_channel = (CorrectionChannel() & key).fetch1('channel') - 1

        if is_bidirectional:
            # Read the ROI
            filename_rel = (experiment.Stack.Filename() & (StackInfo.ROI() & key))
            roi_filename = filename_rel.local_filenames_as_wildcard
            roi = scanreader.read_scan(roi_filename)

            # Compute some parameters
            skip_fields = max(1, int(round(len(field_ids) * 0.10)))
            taper = np.sqrt(np.outer(signal.tukey(image_height, 0.4),
                                     signal.tukey(image_width, 0.4)))

            # Compute raster phase for each slice and take the median
            raster_phases = []
            for field_id in field_ids[skip_fields: -2*skip_fields]:
                # Create template (average frame tapered to avoid edge artifacts)
                slice_ = roi[field_id, :, :, correction_channel, :].astype(np.float32, copy=False)
                anscombed = 2 * np.sqrt(slice_ - slice_.min(axis=(0, 1)) + 3 / 8) # anscombe transform
                template = np.mean(anscombed, axis=-1) * taper

                # Compute raster correction
                raster_phases.append(galvo_corrections.compute_raster_phase(template,
                                                             roi.temporal_fill_fraction))
            raster_phase = np.median(raster_phases)
            raster_std = np.std(raster_phases)
        else:
            raster_phase = 0
            raster_std = 0

        # Insert
        self.insert1({**key, 'raster_phase': raster_phase, 'raster_std': raster_std})

        self.notify(key)

    @notify.ignore_exceptions
    def notify(self, key):
        msg = 'RasterCorrection for `{}` has been populated.'.format(key)
        msg += '\nRaster phase: {}'.format((self & key).fetch1('raster_phase'))
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg)

    def correct(self, roi):
        """ Correct roi with parameters extracted from self. In place.

        :param np.array roi: ROI (fields, image_height, image_width, frames).
        """
        raster_phase = self.fetch1('raster_phase')
        fill_fraction = (StackInfo() & self).fetch1('fill_fraction')
        if abs(raster_phase) < 1e-7:
            corrected = roi.astype(np.float32, copy=False)
        else:
            corrected = roi # in_place
            for i, field in enumerate(roi):
                corrected[i] = galvo_corrections.correct_raster(field, raster_phase, fill_fraction)
        return corrected


@schema
class MotionCorrection(dj.Computed):
    definition = """ # motion correction for each slice in the stack

    -> RasterCorrection
    ---
     y_shifts            : longblob      # y motion correction shifts (num_slices x num_frames)
     x_shifts            : longblob      # x motion correction shifts (num_slices x num_frames)
    """
    @property
    def key_source(self):
        return RasterCorrection() & {'pipe_version': CURRENT_VERSION}

    def _make_tuples(self, key):
        """ Compute motion shifts to align frames over time and over slices."""
        print('Computing motion correction for ROI', key)

        # Get some params
        res = (StackInfo.ROI() & key).fetch1('nframes', 'roi_px_height', 'roi_px_width',
                                             'field_ids')
        num_frames, image_height, image_width, field_ids = res
        correction_channel = (CorrectionChannel() & key).fetch1('channel') - 1

        y_shifts = np.zeros([len(field_ids), num_frames])
        x_shifts = np.zeros([len(field_ids), num_frames])
        if num_frames > 1:
            # Read the ROI
            filename_rel = (experiment.Stack.Filename() & (StackInfo.ROI() & key))
            roi_filename = filename_rel.local_filenames_as_wildcard
            roi = scanreader.read_scan(roi_filename)

            # Compute some params
            skip_rows = int(round(image_height * 0.10))
            skip_cols = int(round(image_width * 0.10))

            # Map: Compute shifts in parallel
            f = performance.parallel_motion_stack # function to map
            raster_phase = (RasterCorrection() & key).fetch1('raster_phase')
            fill_fraction = (StackInfo() & key).fetch1('fill_fraction')
            max_y_shift, max_x_shift = 20 / (StackInfo.ROI() & key).microns_per_pixel
            results = performance.map_fields(f, roi, field_ids=field_ids, channel=correction_channel,
                                             kwargs={'raster_phase': raster_phase, 'fill_fraction': fill_fraction,
                                                     'skip_rows': skip_rows, 'skip_cols': skip_cols,
                                                     'max_y_shift': max_y_shift, 'max_x_shift': max_x_shift})

            # Reduce: Collect results
            for field_idx, y_shift, x_shift in results:
                y_shifts[field_idx] = y_shift
                x_shifts[field_idx] = x_shift

        # Insert
        self.insert1({**key, 'y_shifts': y_shifts, 'x_shifts': x_shifts})

        self.notify(key)

    @notify.ignore_exceptions
    def notify(self, key):
        import seaborn as sns

        y_shifts, x_shifts = (MotionCorrection() & key).fetch1('y_shifts', 'x_shifts')
        fps, is_slow_stack = (StackInfo.ROI() & key).fetch1('fps', 'is_slow')
        num_slices, num_frames = y_shifts.shape
        fps = fps * (num_slices if is_slow_stack else 1)
        seconds = np.arange(num_frames) / fps

        with sns.axes_style('white'):
            fig, axes = plt.subplots(2, 1, figsize=(13, 10), sharex=True, sharey=True)
        axes[0].set_title('Shifts in y for all slices')
        axes[0].set_ylabel('Pixels')
        axes[0].plot(seconds, y_shifts.T)
        axes[1].set_title('Shifts in x for all slices')
        axes[1].set_ylabel('Pixels')
        axes[1].set_xlabel('Seconds')
        axes[1].plot(seconds, x_shifts.T)
        fig.tight_layout()
        img_filename = '/tmp/' + key_hash(key) + '.png'
        fig.savefig(img_filename)
        plt.close(fig)
        sns.reset_orig()

        msg = 'MotionCorrection for `{}` has been populated.'.format(key)
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg, file=img_filename,
                                            file_title='motion shifts')

    def save_as_tiff(self, filename='roi.tif', channel=1):
        """ Correct roi and save as a tiff file.

        :param int channel: What channel to use. Starts at 1
        """
        from tifffile import imsave

        # Get some params
        res = (StackInfo.ROI() & self).fetch1('field_ids', 'roi_px_depth',
                                              'roi_px_height', 'roi_px_width')
        field_ids, px_depth, px_height, px_width = res

        # Load ROI
        roi_filename = (experiment.Stack.Filename() & self).local_filenames_as_wildcard
        roi = scanreader.read_scan(roi_filename)

        # Map: Apply corrections to each field in parallel
        f = performance.parallel_correct_stack # function to map
        raster_phase = (RasterCorrection() & self).fetch1('raster_phase')
        fill_fraction = (StackInfo() & self).fetch1('fill_fraction')
        y_shifts, x_shifts = self.fetch1('y_shifts', 'x_shifts')
        results = performance.map_fields(f, roi, field_ids=field_ids, channel=channel,
                                         kwargs={'raster_phase': raster_phase,
                                                 'fill_fraction': fill_fraction,
                                                 'y_shifts': y_shifts, 'x_shifts': x_shifts})

        # Reduce: Collect results
        corrected_roi = np.empty((px_depth, px_height, px_width), dtype=np.float32)
        for field_idx, corrected_field in results:
            corrected_roi[field_idx] = corrected_field

        print('Saving file at:', filename)
        imsave(filename, corrected_roi)


@schema
class Stitching(dj.Computed):
    definition = """ # stitches together overlapping rois

    -> StackInfo
    ---
    y_direction='up'  : enum('up', 'down')  # whether y points upwards or downwards in the coordinate system
    """
    @property
    def key_source(self):
        # run iff all ROIs have been processed
        stacks = StackInfo() - (StackInfo.ROI() - MotionCorrection())
        return stacks & {'pipe_version': CURRENT_VERSION}

    class Volume(dj.Part):
        definition = """ # union of ROIs from a stack (usually one volume per stack)

        -> Stitching
        volume_id       : tinyint       # id of this volume
        """

    class ROICoordinates(dj.Part):
        definition = """ # coordinates for each ROI in the stitched volume

        -> Stitching                    # animal_id, session, stack_idx, version
        -> MotionCorrection             # animal_id, session, stack_idx, version, roi_id
        ---
        -> Stitching.Volume             # volume to which this ROI belongs
        stitch_xs        : blob         # (px) center of each slice in the volume-wise coordinate system
        stitch_ys        : blob         # (px) center of each slice in the volume-wise coordinate system
        stitch_z         : float        # (um) initial depth in the motor coordinate system
        """

    def _make_tuples(self, key):
        """ Stitch overlapping ROIs together and correct slice-to-slice alignment.

        Iteratively stitches two overlapping ROIs if the overlapping dimension has the
        same length (up to some relative tolerance). Stitching params are calculated per
        slice.

        Edge case: when two overlapping ROIs have different px/micron resolution
            They won't be joined even if true height are the same (as pixel heights will
            not match) or pixel heights could happen to match even if true heights are
            different and thus they'll be erroneously stitched.
        """
        import itertools

        print('Stitching ROIs for stack', key)

        # Get some params
        correction_channel = (CorrectionChannel() & key).fetch1('channel') - 1

        # Read and correct ROIs forming this stack
        print('Correcting ROIs...')
        rois = []
        for roi_tuple in (StackInfo.ROI() & key).fetch():
            # Load ROI
            roi_filename = (experiment.Stack.Filename() & roi_tuple).local_filenames_as_wildcard
            roi = scanreader.read_scan(roi_filename)

            # Map: Apply corrections to each field in parallel
            f = performance.parallel_correct_stack # function to map
            raster_phase = (RasterCorrection() & roi_tuple).fetch1('raster_phase')
            fill_fraction = (StackInfo() & roi_tuple).fetch1('fill_fraction')
            y_shifts, x_shifts = (MotionCorrection() & roi_tuple).fetch1('y_shifts', 'x_shifts')
            field_ids = roi_tuple['field_ids']
            results = performance.map_fields(f, roi, field_ids=field_ids, channel=correction_channel,
                                             kwargs={'raster_phase': raster_phase,
                                                     'fill_fraction': fill_fraction,
                                                     'y_shifts': y_shifts, 'x_shifts': x_shifts,
                                                     'apply_anscombe': True})

            # Reduce: Collect results
            corrected_roi = np.empty((roi_tuple['roi_px_depth'], roi_tuple['roi_px_height'],
                                      roi_tuple['roi_px_width']), dtype=np.float32)
            for field_idx, corrected_field in results:
                corrected_roi[field_idx] = corrected_field

            # Create ROI object
            um_per_px = (StackInfo.ROI() & (StackInfo.ROI().proj() & roi_tuple)).microns_per_pixel
            px_y, px_x = (roi_tuple['roi_y'], roi_tuple['roi_x']) / um_per_px # in pixels
            rois.append(stitching.StitchedROI(corrected_roi, x=px_x, y=px_y,
                                              z=roi_tuple['roi_z'], id_=roi_tuple['roi_id']))

        def join_rows(rois_):
            """ Iteratively join all rois that overlap in the same row."""
            sorted_rois = sorted(rois_, key=lambda roi: (roi.x, roi.y))

            prev_num_rois = float('inf')
            while len(sorted_rois) < prev_num_rois:
                prev_num_rois = len(sorted_rois)

                for left, right in itertools.combinations(sorted_rois, 2):
                    if left.is_aside_to(right):
                        # Compute stitching shifts
                        left_ys, left_xs = [], []
                        for l, r in zip(left.slices, right.slices):
                            delta_y, delta_x = stitching.linear_stitch(l.slice, r.slice, r.x - l.x)
                            left_ys.append(r.y - delta_y)
                            left_xs.append(r.x - delta_x)

                        # Fix outliers
                        roi_key = {**key, 'roi_id': left.roi_coordinates[0].id}
                        max_y_shift, max_x_shift = 15 / (StackInfo.ROI() & roi_key).microns_per_pixel
                        left_ys, left_xs, _ = galvo_corrections.fix_outliers(np.array(left_ys),
                                np.array(left_xs), max_y_shift, max_x_shift, method='trend')

                        # Stitch together
                        right.join_with(left, left_xs, left_ys)
                        sorted_rois.remove(left)
                        break # restart joining

            return sorted_rois

        # Stitch overlapping rois recursively
        print('Computing stitching parameters...')
        prev_num_rois = float('Inf') # to enter the loop at least once
        while len(rois) < prev_num_rois:
            prev_num_rois = len(rois)

            # Join rows
            rois = join_rows(rois)

            # Join columns
            [roi.rot90() for roi in rois]
            rois = join_rows(rois)
            [roi.rot270() for roi in rois]

        # Compute slice-to slice alignment
        print('Computing slice-to-slice alignment...')
        for roi in rois:
            big_volume = roi.volume
            num_slices, image_height, image_width = big_volume.shape

            # Drop 10% of the image borders
            skip_rows = int(round(image_height * 0.1))
            skip_columns = int(round(image_width * 0.1))
            big_volume = big_volume[:, skip_rows:-skip_rows, skip_columns: -skip_columns]

            y_aligns = np.zeros(num_slices)
            x_aligns = np.zeros(num_slices)
            for i in range(1, num_slices):
                # Align current slice to previous one
                y_aligns[i], x_aligns[i] = galvo_corrections.compute_motion_shifts(big_volume[i],
                                                                 big_volume[i-1], in_place=False)

            # Fix outliers
            roi_key = {**key, 'roi_id': roi.roi_coordinates[0].id}
            max_y_shift, max_x_shift = 20 / (StackInfo.ROI() & roi_key).microns_per_pixel
            y_fixed, x_fixed, _ = galvo_corrections.fix_outliers(y_aligns, x_aligns,
                                              max_y_shift, max_x_shift, method='trend')

            # Accumulate shifts so shift i is shift in i -1 plus shift to align i to i-1
            y_cumsum, x_cumsum = np.cumsum(y_fixed), np.cumsum(x_fixed)

            # Detrend to discard influence of vessels going through the slices
            filter_size = int(round(60 / (StackInfo() & key).fetch1('z_step'))) # 60 microns in z
            if len(y_cumsum) > filter_size:
                smoothing_filter = signal.hann(filter_size + (1 if filter_size % 2 == 0 else 0))
                y_detrend = y_cumsum - mirrconv(y_cumsum, smoothing_filter / sum(smoothing_filter))
                x_detrend = x_cumsum - mirrconv(x_cumsum, smoothing_filter / sum(smoothing_filter))
            else:
                y_detrend = y_cumsum - y_cumsum.mean()
                x_detrend = x_cumsum - x_cumsum.mean()

            # Apply alignment shifts in roi
            for slice_, y_align, x_align in zip(roi.slices, y_detrend, x_detrend):
                slice_.y -= y_align
                slice_.x -= x_align
            for roi_coord in roi.roi_coordinates:
                roi_coord.ys = [prev_y - y_align for prev_y, y_align in zip(roi_coord.ys, y_detrend)]
                roi_coord.xs = [prev_x - x_align for prev_x, x_align in zip(roi_coord.xs, x_detrend)]

        # Insert in Stitching
        print('Inserting...')
        self.insert1({**key, 'y_direction': 'down'})

        # Insert each stitched volume
        for volume_id, roi in enumerate(rois):
            self.Volume().insert1({**key, 'volume_id': volume_id + 1})

            # Insert coordinates of each ROI forming this volume
            for roi_coord in roi.roi_coordinates:
                tuple_ = {**key, 'roi_id': roi_coord.id, 'volume_id': volume_id + 1,
                          'stitch_xs': roi_coord.xs, 'stitch_ys': roi_coord.ys,
                          'stitch_z': roi.z}
                self.ROICoordinates().insert1(tuple_)

        self.notify(key)

    @notify.ignore_exceptions
    def notify(self, key):
        notifier = (notify.SlackUser() & (experiment.Session() & key))
        notifier.notify('Stitching for {} has been populated'.format(key))
        for volume_key in (self.Volume() & key).fetch.keys():
            msg = 'Volume {}:'.format(volume_key['volume_id'])
            for roi_coord in (self.ROICoordinates() & volume_key).fetch():
                    roi_id = roi_coord['roi_id']
                    xs, ys = roi_coord['stitch_xs'], roi_coord['stitch_ys']
                    msg += ' ROI {} centered at {:.2f}, {:.2f} (x, y);'.format(roi_id,
                                                                    xs.mean(), ys.mean())
            notifier.notify(msg)


@schema
class CorrectedStack(dj.Computed):
    definition = """ # all slices of each stack after corrections.

    -> Stitching.Volume                 # animal_id, session, stack_idx, volume_id, pipe_version
    ---
    x               : float             # (px) center of volume in a volume-wise coordinate system
    y               : float             # (px) center of volume in a volume-wise coordinate system
    z               : float             # (um) initial depth in the motor coordinate system
    px_height       : smallint          # lines per frame
    px_width        : smallint          # pixels per line
    px_depth        : smallint          # number of slices
    um_height       : float             # height in microns
    um_width        : float             # width in microns
    um_depth        : float             # depth in microns
    """
    @property
    def key_source(self):
        return Stitching.Volume() & {'pipe_version': CURRENT_VERSION}

    class Slice(dj.Part):
        definition = """ # single slice of one stack
        -> CorrectedStack
        -> shared.Channel
        islice              : smallint          # index of slice in volume
        ---
        slice               : longblob          # image (height x width)
        slice_z             : float             # slice depth in volume-wise coordinate system
        """

    def _make_tuples(self, key):
        print('Correcting stack', key)

        for channel in range((StackInfo() & key).fetch1('nchannels')):
            # Correct ROIs
            rois = []
            for roi_tuple in (StackInfo.ROI() * Stitching.ROICoordinates() & key).fetch():
                # Load ROI
                roi_filename = (experiment.Stack.Filename() & roi_tuple).local_filenames_as_wildcard
                roi = scanreader.read_scan(roi_filename)

                # Map: Apply corrections to each field in parallel
                f = performance.parallel_correct_stack # function to map
                raster_phase = (RasterCorrection() & roi_tuple).fetch1('raster_phase')
                fill_fraction = (StackInfo() & key).fetch1('fill_fraction')
                y_shifts, x_shifts = (MotionCorrection() & roi_tuple).fetch1('y_shifts', 'x_shifts')
                field_ids = roi_tuple['field_ids']
                results = performance.map_fields(f, roi, field_ids=field_ids, channel=channel,
                                                 kwargs={'raster_phase': raster_phase,
                                                         'fill_fraction': fill_fraction,
                                                         'y_shifts': y_shifts, 'x_shifts': x_shifts})

                # Reduce: Collect results
                corrected_roi = np.empty((roi_tuple['roi_px_depth'], roi_tuple['roi_px_height'],
                                          roi_tuple['roi_px_width']), dtype=np.float32)
                for field_idx, corrected_field in results:
                    corrected_roi[field_idx] = corrected_field

                # Create ROI object
                xs, ys = list(roi_tuple['stitch_xs']), list(roi_tuple['stitch_ys'])
                rois.append(stitching.StitchedROI(corrected_roi, x=xs, y=ys, z=roi_tuple['stitch_z'],
                                                  id_=roi_tuple['roi_id']))

            def join_rows(rois_):
                """ Iteratively join all rois that overlap in the same row."""
                sorted_rois = sorted(rois_, key=lambda roi: (roi.x, roi.y))

                prev_num_rois = float('inf')
                while len(sorted_rois) < prev_num_rois:
                    prev_num_rois = len(sorted_rois)

                    for left, right in itertools.combinations(sorted_rois, 2):
                        if left.is_aside_to(right):
                            left_xs = [s.x for s in left.slices]
                            left_ys = [s.y for s in left.slices]
                            right.join_with(left, left_xs, left_ys)
                            sorted_rois.remove(left)
                            break # restart joining

                return sorted_rois

            # Stitch all rois together. This is convoluted because smooth blending in
            # join_with assumes rois are next to (not below or atop of) each other
            prev_num_rois = float('Inf') # to enter the loop at least once
            while len(rois) < prev_num_rois:
                prev_num_rois = len(rois)

                # Join rows
                rois = join_rows(rois)

                # Join columns
                [roi.rot90() for roi in rois]
                rois = join_rows(rois)
                [roi.rot270() for roi in rois]

            # Check stitching went alright
            if len(rois) > 1:
                msg = 'ROIs for volume {} could not be stitched properly'.format(key)
                raise PipelineException(msg)
            stitched = rois[0]

            # Insert in CorrectedStack
            roi_info = StackInfo.ROI() & key & {'roi_id': stitched.roi_coordinates[0].id} # one roi from this volume
            tuple_ = {**key, 'x': stitched.x, 'y': stitched.y, 'z': stitched.z,
                      'px_height': stitched.height, 'px_width': stitched.width}
            tuple_['um_height'] = stitched.height * roi_info.microns_per_pixel[0]
            tuple_['um_width'] = stitched.width * roi_info.microns_per_pixel[1]
            tuple_['px_depth'] = roi_info.fetch1('roi_px_depth') # same as original rois
            tuple_['um_depth'] = roi_info.fetch1('roi_um_depth') # same as original rois
            self.insert1(tuple_, skip_duplicates=True)

            # Insert each slice
            initial_z = stitched.z
            z_step = (StackInfo() & key).fetch1('z_step')
            for i, slice_ in enumerate(stitched.volume):
                self.Slice().insert1({**key, 'channel': channel + 1, 'islice': i + 1,
                                      'slice': slice_, 'slice_z': initial_z + i * z_step})

            self.notify({**key, 'channel': channel + 1})

    @notify.ignore_exceptions
    def notify(self, key):
        import imageio

        volume = (self & key).get_stack(channel=key['channel'])
        volume = volume[:: int(volume.shape[0] / 8)] # volume at 8 diff depths
        video_filename = '/tmp/' + key_hash(key) + '.gif'
        imageio.mimsave(video_filename, float2uint8(volume), duration=1)

        msg = 'CorrectedStack for {} has been populated.'.format(key)
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg, file=video_filename,
                                                                   file_title='stitched ROI')

    def get_stack(self, channel=1):
        """ Get full stack (num_slices, height, width).

        :param int channel: What channel to use. Starts at 1

        :returns The stack: a (num_slices, image_height, image_width) array.
        :rtype: np.array (float32)
        """
        slice_rel = (CorrectedStack.Slice() & self & {'channel': channel})
        slices = slice_rel.fetch('slice', order_by='islice')
        return np.stack(slices)

    def save_as_tiff(self, filename='stack.tif', channel=1):
        """ Save current stack as a tiff file.

        :param int channel: What channel to use. Starts at 1
        """
        from tifffile import imsave
        print('Saving file at:', filename)
        imsave(filename, self.get_stack(channel=channel))

    def save_video(self, filename='stack.mp4', channel=1, fps=10, dpi=250):
        """ Creates an animation video showing a fly-over of the stack (top to bottom).

        :param string filename: Output filename (path + filename)
        :param int channel: What channel to use. Starts at 1
        :param int start_index: Where in the scan to start the video.
        :param int fps: Number of slices shown per second.
        :param int dpi: Dots per inch, controls the quality of the video.

        :returns Figure. You can call show() on it.
        :rtype: matplotlib.figure.Figure
        """
        from matplotlib import animation

        stack = self.get_stack(channel=channel)
        num_slices = stack.shape[0]

        fig = plt.figure()
        fig, axes = plt.subplots(1, 1, sharex=True, sharey=True)
        im = fig.gca().imshow(stack[int(num_slices / 2)])
        video = animation.FuncAnimation(fig, lambda i: im.set_data(stack[i]), num_slices,
                                        interval=1000 / fps)
        fig.tight_layout()

        if not filename.endswith('.mp4'):
            filename += '.mp4'
        print('Saving video at:', filename)
        print('If this takes too long, stop it and call again with dpi <', dpi, '(default)')
        video.save(filename, dpi=dpi)

        return fig


@schema
class RegistrationTask(dj.Manual):
    definition = """ # declare scan fields to register to a stack as well as channels and method used
    (stack_session) -> CorrectedStack(session)  # animal_id, stack_session, stack_idx, pipe_version, volume_id
    (scan_session) -> experiment.Scan(session)  # animal_id, scan_session, scan_idx
    -> shared.Field
    (stack_channel) -> shared.Channel(channel)
    (scan_channel) -> shared.Channel(channel)
    -> shared.RegistrationMethod
    """
    def fill_in(self, stack_key, scan_key, stack_channel=1, scan_channel=1, method=1):
        # Add stack attributes
        stack_rel = CorrectedStack() & stack_key
        if len(stack_rel) > 1:
            raise PipelineException('More than one stack match stack_key {}'.format(stack_key))
        tuple_ = stack_rel.proj(stack_session='session').fetch1()

        # Add common attributes
        tuple_['stack_channel'] = stack_channel
        tuple_['scan_channel'] = scan_channel
        tuple_['registration_method'] = method

        # Add scan attributes
        fields_rel = reso.ScanInfo.Field().proj() + meso.ScanInfo.Field().proj() & scan_key
        scan_animal_ids = np.unique(fields_rel.fetch('animal_id'))
        if len(scan_animal_ids) > 1 or scan_animal_ids[0] != tuple_['animal_id']:
            raise PipelineException('animal_id of stack and scan do not match.')
        for field in fields_rel.fetch():
            RegistrationTask().insert1({**tuple_, 'scan_session': field['session'],
                                        'scan_idx': field['scan_idx'],
                                        'field': field['field']}, skip_duplicates=True)


def _create_rotation_matrix(alpha, beta, gamma):
    """ 3-D rotation matrix to apply a intrinsic yaw-> pitch-> roll rotation.

    We use a right handed coordinate system (x points to the right, y towards you, and z
    downward) with right-handed/clockwise rotations.

    :param float alpha: Angle in degrees for rotation over z axis in degress (yaw)
    :param float beta: Angle in degrees for rotation over y axis in degress (pitch)
    :param float gamma: Angle in degrees for rotation over x axis in degress (roll)

    :returns: (3, 3) rotation matrix

    ..ref:: danceswithcode.net/engineeringnotes/rotations_in_3d/rotations_in_3d_part1.html
    """
    # Rotation matrix from here:
    #    danceswithcode.net/engineeringnotes/rotations_in_3d/rotations_in_3d_part1.html
    w, v, u = alpha *np.pi/180, beta * np.pi/180, gamma * np.pi/180 # degrees to radians
    sin, cos = np.sin, np.cos
    rotation_matrix = [
        [cos(v)*cos(w), sin(u)*sin(v)*cos(w) - cos(u)*sin(w), sin(u)*sin(w) + cos(u)*sin(v)*cos(w)],
        [cos(v)*sin(w), cos(u)*cos(w) + sin(u)*sin(v)*sin(w), cos(u)*sin(v)*sin(w) - sin(u)*cos(w)],
        [-sin(v),       sin(u)*cos(v),                        cos(u)*cos(v)]
    ]
    return rotation_matrix

def find_intersecting_point(p1, p2, z):
    """ Find a point at a given z in the line that crosses p1 and p2.

    :param np.array p1: A point (1-d array of size 3) or a matrix of points (3 x n).
    :param np.array p2: A point (1-d array of size 3) or a matrix of points (3 x n).
        Number of points in p2 needs to match p1.
    :param float z: z to insersect the line.

    :returns: A point (1-d array of size 3) or a matrix of points (3 x n).

    ..ref:: See https://brilliant.org/wiki/3d-coordinate-geometry-equation-of-a-line/
    """
    direction_vector = p2 - p1
    if any(abs(direction_vector[2]) < 1e-10):
        raise ArithmeticError('Line is parallel to the z-plane. Infinite or no solutions.')
    q = p1 + ((z - p1[2]) / direction_vector[2]) * direction_vector # p1 + ((z-z1)/ n) * d
    return q


#TODO: Move to performance
def parallel_registration(angles, stack, field):
    """ Registers using skimage.feature match_template.

    Rotates the stack, cross-correlates the field at each position and returns the score,
    coordinates and angles of the best one. We use a right handed coordinate system (x
    points to the right, y towards you, and z downward) with right-handed/clockwise
    rotations.

    :param triplet angles: A triplet with the angle in degrees for rotation over z (yaw),
        y (pitch) and x (roll).
    :param np.array stack: 3-d stack. Depth x height x width.
    :param np.array field: A single field. Height x width.

    : returns: The best score, a triplet of angles (yaw, pitch, roll) and a triplet of
        coordinates (x, y, z)

    .. note:: Assumes field height/width is smaller than stack height/width.
    """
    from skimage import feature
    from scipy import ndimage

    # Rotate stack (inverse of intrinsic yaw -> pitch -> roll= extrinsic yaw -> pitch -> roll with flipped signs)
    rotated = ndimage.rotate(stack, angles[0], axes=(1, 2), order=1, reshape=False) # yaw(-w)
    rotated = ndimage.rotate(rotated, -angles[1], axes=(0, 2), order=1, reshape=False) # pitch(-v)
    rotated = ndimage.rotate(rotated, angles[2], axes=(0, 1), order=1, reshape=False) # roll(-u)
    # Note on ndimage.rotate: Assuming our coordinate system (z points towards
    # the screen, y downwards and x to the right). Axes (1, 2) will do a left
    # handed rotation in z, (0, 2) is a right handed rotation in y and (0, 1)
    # is a left-handed rotation in x.

     # 3-d match_template
    corrs = np.stack(feature.match_template(s, field, pad_input=True) for s in rotated)
    smooth_corrs = ndimage.gaussian_filter(corrs, 0.7)
    score = np.max(smooth_corrs)
    z, y, x = np.unravel_index(np.argmax(smooth_corrs), smooth_corrs.shape)

    return score, angles, (x, y, z)


@schema
class FieldRegistration(dj.Computed):
    """
    Note: We stick with this conventions to define rotations:
    http://danceswithcode.net/engineeringnotes/rotations_in_3d/rotations_in_3d_part1.html

    "To summarize, we will employ a Tait-Bryan Euler angle convention using active,
    intrinsic rotations around the axes in the order z-y-x". We use a right-handed
    coordinate system (x points to the right, y points forward and z points downwards)
    with right-handed/clockwise rotations.

    To register the field to the stack:
        1. Scale the field & stack to have isotropic pixels & voxels that match the
            lowest pixels-per-microns resolution among the two (common_res).
        2. Rotate the field over x -> y -> z (extrinsic roll->pitch->yaw is equivalent to
            an intrinsic yaw->pitch-roll rotation) using clockwise rotations and taking
            center of the field as (0, 0, 0).
        3. Translate to final x, y, z position (accounting for the previous stack scaling).
    """
    definition = """ # align a 2-d scan field to a stack
    -> RegistrationTask
    ---
    reg_x       : float         # (px) center of scan in stack coordinates
    reg_y       : float         # (px) center of scan in stack coordinates
    reg_z       : float         # (um) depth of scan in stack coordinates
    yaw=0       : float         # degrees of rotation over the z axis
    pitch=0     : float         # degrees of rotation over the y axis
    roll=0      : float         # degrees of rotation over the x axis
    score       : float         # cross-correlation score (-1 to 1)
    common_res  : float         # (um/px) common resolution used for registration
    """
    @property
    def key_source(self):
        processed_fields = (reso.SummaryImages() + meso.SummaryImages()).proj(scan_session='session')
        return RegistrationTask() & processed_fields & {'pipe_version': CURRENT_VERSION}

    def _make_tuples(self, key):
        from scipy import ndimage

        print('Registering', key)

        # Get stack
        stack_rel = (CorrectedStack() & key & {'session': key['stack_session']})
        stack = stack_rel.get_stack(key['stack_channel'])

        # Get average field
        field_key = {'animal_id': key['animal_id'], 'session': key['scan_session'],
                     'scan_idx': key['scan_idx'], 'field': key['field'],
                     'channel': key['scan_channel']} # no pipe_version
        pipe = reso if reso.ScanInfo() & field_key else meso if meso.ScanInfo() & field_key else None
        field = (pipe.SummaryImages.Average() & field_key).fetch1('average_image')

        # Drop some edges (only y and x) to avoid artifacts (and black edges in stacks)
        skip_dims = np.clip(np.round(np.array(stack.shape) * 0.025), 1, None).astype(int)
        stack = stack[:, skip_dims[1] : -skip_dims[1], skip_dims[2]: -skip_dims[2]]
        skip_dims = np.clip(np.round(np.array(field.shape) * 0.025), 1, None).astype(int)
        field = field[skip_dims[0] : -skip_dims[0], skip_dims[1]: -skip_dims[1]]

        # Rescale to match lowest resolution  (isotropic pixels/voxels)
        field_res = ((reso.ScanInfo() & field_key).microns_per_pixel if pipe == reso else
                     (meso.ScanInfo.Field() & field_key).microns_per_pixel)
        dims = stack_rel.fetch1('um_depth', 'px_depth', 'um_height', 'px_height',
                                'um_width', 'px_width')
        stack_res = np.array([dims[0] / dims[1], dims[2] / dims[3], dims[4] / dims[5]])
        common_res = max(*field_res, *stack_res) # minimum available resolution
        stack = ndimage.zoom(stack, stack_res / common_res, order=1)
        field = ndimage.zoom(field, field_res / common_res, order=1)

        # Get estimated depth of the field (from experimenters)
        stack_x, stack_y, stack_z = stack_rel.fetch1('x', 'y', 'z') # z of the first slice (zero is at surface depth)
        field_z = (pipe.ScanInfo.Field() & field_key).fetch1('z') # measured in microns (zero is at surface depth)
        if field_z < stack_z or field_z > stack_z + dims[0]:
            msg_template = 'Warning: Estimated depth ({}) outside stack range ({}-{}).'
            print(msg_template.format(field_z, stack_z , stack_z + dims[0]))
        estimated_px_z = (field_z - stack_z + 0.5) / common_res # in pixels

        # Register
        z_range = 40 / common_res # search 40 microns up and down
        if key['registration_method'] == 1: # rigid
            # Restrict to relevant part of the scan
            mini_stack = stack[max(0, int(round(estimated_px_z - z_range))): int(round(estimated_px_z + z_range))]

            # Crop field FOV to be smaller than the stack's
            cut_rows = max(1, int(np.ceil((field.shape[0] - mini_stack.shape[1]) / 2)))
            cut_cols = max(1, int(np.ceil((field.shape[1] - mini_stack.shape[2]) / 2)))
            field = field[cut_rows:-cut_rows, cut_cols:-cut_cols]

            # 3-d match_template
            best_score, _, (x, y, z) = parallel_registration((0, 0, 0), mini_stack, field)

            # Rewrite offsets as stack coordinates
            final_x = stack_x + ((x + 0.5) - mini_stack.shape[2] / 2) * (common_res / stack_res[2]) # in stack pixels
            final_y = stack_y + ((y + 0.5) - mini_stack.shape[1] / 2) * (common_res / stack_res[1]) # in stack pixels
            final_z = stack_z + (max(0, int(round(estimated_px_z - z_range))) + z + 0.5) * common_res # in microns
            #Note: Best match in the first slice will return z = 0.5 * z_step and not 0.

            self.insert1({**key, 'reg_x': final_x, 'reg_y': final_y, 'reg_z': final_z,
                          'score': best_score, 'common_res': common_res})

        elif key['registration_method'] == 2: # rigid plus 3-d rotation
            import multiprocessing as mp
            from itertools import product
            from functools import partial

            max_angle = 5 # max angle in degrees to try for rotations



            #TODO: Delete this
            yaw, pitch, roll = 2, 3, 4



            # Get rotation matrix and its inverse
            R = _create_rotation_matrix(yaw, pitch, roll)
            R_inv = np.linalg.inv(R)
            # We'll rotate the stack with the inverse of an intrinsic yaw->pitch-> roll rotation

            # Compute highest z after rotation (accounting for z_range)
            h, w = stack.shape[1] / 2, stack.shape[2] / 2 # y, x
            corners_at_zrange = [[-w, -h, z_range], [w, -h, z_range], [w, h, z_range],
                                 [-w, h, z_range]] # four stack corners at z=z_range
            rot_corners_at_zrange = np.dot(R_inv, np.array(corners_at_zrange).T)
            rot_z_top = np.max(rot_corners_at_zrange[2])

            # We'll cut the rotated stack at rot_z_top and we want to know how many more
            # slices above rot_z_top are needed to avoid black spaces at the edges.
            # We find all corners of the cross-section at z=rot_z_top, map it back to
            # our original coordinate system and find the maximum z.
            corners_at_zero = [[-w, -h, 0], [w, -h, 0], [w, h, 0], [-w, h, 0]] # clockwise, starting at upper left
            corners_at_100= [[-w, -h, 100], [w, -h, 100], [w, h, 100], [-w, h, 100]]
            rot_corners_at_zero = np.dot(R_inv, np.array(corners_at_zero).T) #
            rot_corners_at_100 = np.dot(R_inv, np.array(corners_at_100).T)
            rot_corners_at_ztop = find_intersecting_point(rot_corners_at_zero, rot_corners_at_100, rot_z_top)
            corners_at_ztop = np.dot(R, rot_corners_at_ztop)
            z_slack = np.max(corners_at_ztop[2])

            # Restrict stack to relevant part of the scan
            mini_stack = stack[max(0, int(round(estimated_px_z - z_slack))): int(round(estimated_px_z + z_slack))]

            # Rotate stack (inverse of intrinsic yaw-> pitch -> roll)
            rotated = ndimage.rotate(mini_stack, yaw, axes=(1, 2), order=1) # yaw(-w)
            rotated = ndimage.rotate(rotated, -pitch, axes=(0, 2), order=1) # pitch(-v)
            rotated = ndimage.rotate(rotated, roll, axes=(0, 1), order=1) # roll(-u)

            # Compute amount of cut in z
            est_z = (estimated_px_z - max(0, int(round(estimated_px_z - z_slack)))) - mini_stack.shape[0] / 2
            rot_est_z = np.dot(R_inv, [0, 0, est_z])[2]
            min_z, max_z = rot_est_z - rot_z_top, rot_est_z + rot_z_top

            # Compute amount of x and y slack
            top_corners = find_intersecting_point(rot_corners_at_zero, rot_corners_at_100, max(-rotated.shape[0] / 2, min_z))
            bottom_corners = find_intersecting_point(rot_corners_at_zero, rot_corners_at_100, min(rotated.shape[0] / 2, max_z))
            min_x = max(*top_corners[0, [0, 3]], *bottom_corners[0, [0, 3]])
            max_x = min(*top_corners[0, [1, 2]], *bottom_corners[0, [1, 2]])
            min_y = max(*top_corners[1, [0, 1]], *bottom_corners[1, [0, 1]])
            max_y = min(*top_corners[1, [2, 3]], *bottom_corners[1, [2, 3]])

            # Cut rotated stack
            mini_rotated = rotated[max(0, int(round(rotated.shape[0] / 2 + min_z))): int(round(rotated.shape[0] / 2 + max_z)),
                                   max(0, int(round(rotated.shape[1] / 2 + min_y))): int(round(rotated.shape[1] / 2 + max_y)),
                                   max(0, int(round(rotated.shape[2] / 2 + min_x))): int(round(rotated.shape[2] / 2 + max_x))]
            z_center = rotated.shape[0] / 2 - max(0, int(round(rotated.shape[0] / 2 + min_z)))  # z of the center with zero at the top of mini_rotated
            y_center = rotated.shape[1] / 2 - max(0, int(round(rotated.shape[1] / 2 + min_y)))
            x_center = rotated.shape[2] / 2 - max(0, int(round(rotated.shape[2] / 2 + min_x)))

            # Crop field FOV to be smaller than the stack's
            cut_rows = max(1, int(np.ceil((field.shape[0] - mini_rotated.shape[1]) / 2)))
            cut_cols = max(1, int(np.ceil((field.shape[1] - mini_rotated.shape[2]) / 2)))
            field = field[cut_rows:-cut_rows, cut_cols:-cut_cols]

            # 3-d match_template
            corrs = np.stack(feature.match_template(s, field, pad_input=True) for s in mini_rotated)
            smooth_corrs = ndimage.gaussian_filter(corrs, 0.7)
            best_score = np.max(smooth_corrs)
            z, y, x = np.unravel_index(np.argmax(smooth_corrs), smooth_corrs.shape)

            x_offset = (x + 0.5) - x_center
            y_offset = (y + 0.5) - y_center
            z_offset = (z + 0.5) - z_center

            # Map back to original coordinates
            xp, yp, zp = np.dot(R, [x_offset, y_offset, z_offset])
            zp = ((max(0, int(round(estimated_px_z - z_slack))) + mini_stack.shape[0] / 2
                   + zp) - stack.shape[0] / 2) # with respect to original z center

            # Map back to stack coordinates
            final_x = stack_x + xp * (common_res / stack_res[2]) # in stack pixels
            final_y = stack_y + yp * (common_res / stack_res[1]) # in stack pixels
            final_z = stack_z + (zp + stack.shape[0] / 2) * common_res # in microns


            self.insert1({**key, 'reg_x': final_x, 'reg_y': final_y, 'reg_z': final_z,
                          'yaw': yaw, 'pitch': pitch, 'roll': roll, 'score': best_score,
                          'common_res': common_res})
        #self.notify(key)

    @notify.ignore_exceptions
    def notify(self, key):
        x, y, z = (FieldRegistration() &  key).fetch('reg_x', 'reg_y', 'reg_z')
        yaw, pitch, roll = (FieldRegistration() &  key).fetch('yaw', 'pitch', 'roll')
        f_string = ('FieldRegistration for {} has been populated. Field registered at {},'
                    '{}, {} (z, y, x) with {}, {}, {} (yaw, pitch, roll) inclination')
        msg = f_string.format(key, z, y, x, yaw, pitch, roll)
        (notify.SlackUser() & (experiment.Session() & key)).notify(msg)

# TODO: Add this in shared
#class SegmentationMethod(dj.Lookup):
#    defintion = """ # 3-d segmentation methods
#    """
#    # threshold: Just threshold the scan and postprocess (nuclear labels)
#    # blob: Gaussian blob detection
#    # covnent: 3-d convnet
#
#@schema
#class SegmentationTask(dj.Manual):
#    definition = """ # defines the target of segmentation and the channel to use
#
#    -> experiment.Stack
#    -> shared.Channel
#    -> shared.SegmentationMethod
#    ---
#    -> experiment.Compartment
#    """
