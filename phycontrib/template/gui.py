# -*- coding: utf-8 -*-

"""Template GUI."""


#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import logging
import os.path as op

import numpy as np
import scipy.io as sio

from phy.cluster.manual import ManualClustering
from phy.cluster.manual.controller import Controller
from phy.cluster.manual.views import (select_traces, ScatterView)

from phy.gui import create_gui
from phy.io.array import concat_per_cluster
from phy.traces import SpikeLoader, WaveformLoader
from phy.traces.filter import apply_filter, bandpass_filter
from phy.utils import Bunch

from phycontrib.kwik.model import _concatenate_virtual_arrays
from phycontrib.csicsvari.traces import read_dat

logger = logging.getLogger(__name__)


#------------------------------------------------------------------------------
# Template views
#------------------------------------------------------------------------------

def subtract_templates(traces,
                       start=None,
                       spike_times=None,
                       spike_clusters=None,
                       amplitudes=None,
                       spike_templates=None,
                       sample_rate=None,
                       ):
    traces = traces.copy()
    st = spike_times
    w = spike_templates * amplitudes[:, np.newaxis, np.newaxis]
    n = traces.shape[0]
    for index in range(w.shape[0]):
        t = int(round((st[index] - start) * sample_rate))
        i, j = 20, 41
        x = w[index]  # (n_samples, n_channels)
        sa, sb = t - i, t + j
        if sa < 0:
            x = x[-sa:, :]
            sa = 0
        elif sb > n:
            x = x[:-(sb - n), :]
            sb = n
        traces[sa:sb, :] -= x
    return traces


class AmplitudeView(ScatterView):
    pass


class FeatureTemplateView(ScatterView):
    pass


#------------------------------------------------------------------------------
# Template Controller
#------------------------------------------------------------------------------

filenames = {
    'amplitudes': 'amplitudes.npy',
    'spike_clusters': 'clusterIDs.npy',
    'templates': 'templates.npy',
    'spike_samples': 'spikeTimes.npy',
    'channel_mapping': 'chanMap0ind.npy',
    'channel_positions_x': 'xcoords.npy',
    'channel_positions_y': 'ycoords.npy',
    'whitening_matrix': 'whiteningMatrix.npy',

    'features': 'pcFeatures.npy',
    'features_ind': 'pcFeatureInds.npy',
    'template_features': 'templateFeatures.npy',
    'template_features_ind': 'templateFeatureInds.npy',
}


def read_array(name):
    fn = filenames[name]
    arr_name, ext = op.splitext(fn)
    if ext == '.mat':
        return sio.loadmat(fn)[arr_name]
    elif ext == '.npy':
        return np.load(fn)


def get_masks(templates):
    n_templates, n_samples_templates, n_channels = templates.shape
    templates = np.abs(templates)
    m = templates.max(axis=1)  # (n_templates, n_channels)
    mm = m.max(axis=1)  # (n_templates,
    masks = m / mm[:, np.newaxis]  # (n_templates, n_channels)
    masks[mm == 0, :] = 0
    return masks


class MaskLoader(object):
    def __init__(self, cluster_masks, spike_clusters):
        self._spike_clusters = spike_clusters
        self._cluster_masks = cluster_masks
        self.shape = (len(spike_clusters), cluster_masks.shape[1])

    def __getitem__(self, item):
        # item contains spike ids
        clu = self._spike_clusters[item]
        return self._cluster_masks[clu]


class TemplateController(Controller):
    def __init__(self, dat_path, **kwargs):
        path = op.realpath(op.expanduser(dat_path))
        self.cache_dir = op.join(op.dirname(path), '.phy')
        self.dat_path = dat_path
        self.__dict__.update(kwargs)
        super(TemplateController, self).__init__()

    def _init_data(self):
        traces = read_dat(self.dat_path,
                          n_channels=self.n_channels_dat,
                          dtype=self.dtype or np.int16,
                          )

        n_samples_t, _ = traces.shape
        assert _ == self.n_channels_dat

        amplitudes = read_array('amplitudes').squeeze()
        n_spikes, = amplitudes.shape

        spike_clusters = read_array('spike_clusters').squeeze()
        spike_clusters = spike_clusters.astype(np.int32)
        assert spike_clusters.shape == (n_spikes,)

        spike_samples = read_array('spike_samples').squeeze()
        assert spike_samples.shape == (n_spikes,)

        templates = read_array('templates')
        templates[np.isnan(templates)] = 0
        templates = np.transpose(templates, (2, 1, 0))
        n_templates, n_samples_templates, n_channels = templates.shape

        channel_mapping = read_array('channel_mapping').squeeze()
        channel_mapping = channel_mapping.astype(np.int32)
        assert channel_mapping.shape == (n_channels,)

        channel_positions = np.c_[read_array('channel_positions_x'),
                                  read_array('channel_positions_y')]
        assert channel_positions.shape == (n_channels, 2)

        all_features = np.load(filenames['features'], mmap_mode='r')
        features_ind = read_array('features_ind').astype(np.int32)

        self.all_features = all_features
        self.features_ind = features_ind

        template_features = np.load(filenames['template_features'],
                                    mmap_mode='r')
        template_features_ind = read_array('template_features_ind'). \
            astype(np.int32)
        template_features_ind = template_features_ind.T.copy()

        self.n_channels = n_channels
        # Take dead channels into account.
        traces = _concatenate_virtual_arrays([traces], channel_mapping)
        self.n_spikes = n_spikes

        # Amplitudes
        self.all_amplitudes = amplitudes
        self.amplitudes_lim = self.all_amplitudes.max()

        # Templates
        self.templates = templates
        self.n_samples_templates = n_samples_templates
        self.template_lim = np.max(np.abs(self.templates))
        self.n_templates = len(self.templates)

        self.duration = n_samples_t / float(self.sample_rate)

        self.spike_times = spike_samples / float(self.sample_rate)
        assert np.all(np.diff(self.spike_times) >= 0)

        self.spike_clusters = spike_clusters
        self.cluster_ids = np.unique(self.spike_clusters)
        n_clusters = len(self.cluster_ids)
        self.channel_positions = channel_positions
        self.all_traces = traces

        self.whitening_matrix = read_array('whitening_matrix')

        # Filter the waveforms.
        order = 3
        filter_margin = order * 3
        b_filter = bandpass_filter(rate=self.sample_rate,
                                   low=500.,
                                   high=self.sample_rate * .475,
                                   order=order)

        def the_filter(x, axis=0):
            return apply_filter(x, b_filter, axis=axis)

        # Fetch waveforms from traces.
        nsw = self.n_samples_waveforms
        waveforms = WaveformLoader(traces=traces,
                                   n_samples_waveforms=nsw,
                                   filter=the_filter,
                                   filter_margin=filter_margin,
                                   )
        waveforms = SpikeLoader(waveforms, spike_samples)
        self.all_waveforms = waveforms

        self.template_masks = get_masks(templates)
        self.all_masks = MaskLoader(self.template_masks, spike_clusters)

        self.n_features_per_channel = 3
        # TODO
        self.cluster_groups = {c: None for c in range(n_clusters)}

        # Check sparse features arrays shapes.
        assert all_features.ndim == 3
        n_loc_chan = all_features.shape[2]
        assert all_features.shape == (self.n_spikes,
                                      self.n_features_per_channel,
                                      n_loc_chan,
                                      )
        assert features_ind.shape == (n_loc_chan, self.n_templates)

        n_sim_tem = template_features.shape[1]
        assert template_features.shape == (n_spikes, n_sim_tem)
        assert template_features_ind.shape == (n_templates, n_sim_tem)
        self.template_features_ind = template_features_ind
        self.template_features = template_features

        # Unwhiten the templates.
        wmi = np.linalg.inv(self.whitening_matrix / 200.)
        self.templates_unw = np.dot(self.templates, wmi)

    def get_background_features(self):
        # Disable for now
        pass

    def _init_context(self):
        super(TemplateController, self)._init_context()
        ctx = self.context
        self.get_amplitudes = ctx.cache(self.get_amplitudes)
        self.get_template_features = ctx.cache(self.get_template_features)

    @concat_per_cluster
    def get_features(self, cluster_id):
        spike_ids = self._select_spikes(cluster_id, 1000)
        nc = self.n_channels
        nfpc = self.n_features_per_channel
        ns = len(spike_ids)
        shape = (ns, nc, nfpc)
        f = np.zeros(shape)
        # Sparse channels.
        ch = self.features_ind[:, cluster_id]
        # Populate the dense features array.
        f[:, ch, :] = np.transpose(self.all_features[spike_ids, :, :],
                                   (0, 2, 1))
        b = Bunch()
        b.data = f
        b.spike_ids = spike_ids
        b.spike_clusters = self.spike_clusters[spike_ids]
        b.masks = self.all_masks[spike_ids]
        return b

    @concat_per_cluster
    def get_amplitudes(self, cluster_id):
        spike_ids = self._select_spikes(cluster_id, 10000)
        d = Bunch()
        d.spike_ids = spike_ids
        d.spike_clusters = cluster_id * np.ones(len(spike_ids), dtype=np.int32)
        d.x = self.spike_times[spike_ids]
        d.y = self.all_amplitudes[spike_ids]
        return d

    def get_template_features(self, cluster_ids):
        template_features = self.template_features
        template_features_ind = self.template_features_ind
        d = Bunch()
        if len(cluster_ids) < 2:
            return None
        cx, cy = map(int, cluster_ids[:2])
        sim_x = template_features_ind[cx].tolist()
        sim_y = template_features_ind[cy].tolist()
        if cx not in sim_y or cy not in sim_x:
            return None
        sxy = sim_x.index(cy)
        syx = sim_y.index(cx)
        spikes_x = self._select_spikes(cx)
        spikes_y = self._select_spikes(cy)
        spike_ids = np.hstack([spikes_x, spikes_y])
        d.x = np.hstack([template_features[spikes_x, 0],
                         template_features[spikes_y, syx]])
        d.y = np.hstack([template_features[spikes_x, sxy],
                         template_features[spikes_y, 0]])
        d.spike_ids = spike_ids
        d.spike_clusters = self.spike_clusters[spike_ids]
        return d

    def get_traces(self, interval):
        """Load traces and spikes in an interval."""
        tr = select_traces(self.all_traces, interval,
                           sample_rate=self.sample_rate,
                           )
        tr = tr - np.mean(tr, axis=0)

        a, b = self.spike_times.searchsorted(interval)
        sc = self.spike_clusters[a:b]

        # Remove templates.
        tr_sub = subtract_templates(tr,
                                    start=interval[0],
                                    spike_times=self.spike_times[a:b],
                                    spike_clusters=sc,
                                    amplitudes=self.all_amplitudes[a:b],
                                    spike_templates=self.templates_unw[sc],
                                    sample_rate=self.sample_rate,
                                    )

        return [Bunch(traces=tr),
                Bunch(traces=tr_sub, color=(.25, .25, .25, .75))]

    def similarity(self, cluster_id):
        n = self.template_features_ind.shape[1]
        sim0 = self.template_features_ind[cluster_id]
        sim = [(int(c), -n + i) for i, c in enumerate(sim0)]
        sim2 = self.get_close_clusters(cluster_id)
        sim2 = [_ for _ in sim2 if _[0] not in sim0]
        sim.extend(sim2)
        return sim

    def set_manual_clustering(self, gui):
        mc = ManualClustering(self.spike_clusters,
                              self.spikes_per_cluster,
                              similarity=self.similarity,
                              cluster_groups=self.cluster_groups,
                              )
        self.manual_clustering = mc
        mc.add_column(self.get_probe_depth)
        mc.attach(gui)

    def add_amplitude_view(self, gui):
        view = AmplitudeView(coords=self.get_amplitudes,
                             )
        view.attach(gui)
        return view

    def add_feature_template_view(self, gui):
        view = FeatureTemplateView(coords=self.get_template_features,
                                   )
        view.attach(gui)
        return view


#------------------------------------------------------------------------------
# Template GUI
#------------------------------------------------------------------------------

def create_template_gui(dat_path=None, plugins=None, **kwargs):
    controller = TemplateController(dat_path, **kwargs)
    # Create the GUI.
    gui = create_gui(name='TemplateGUI',
                     subtitle=dat_path,
                     plugins=plugins,
                     )
    controller.set_manual_clustering(gui)
    controller.add_waveform_view(gui)
    controller.add_feature_view(gui)
    controller.add_feature_template_view(gui)
    controller.add_amplitude_view(gui)
    controller.add_trace_view(gui)
    controller.add_correlogram_view(gui)

    # Save.
    @gui.connect_
    def on_request_save(spike_clusters, groups):
        # TODO
        pass

    return gui