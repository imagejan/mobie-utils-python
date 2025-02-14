import os
import warnings

import luigi
import numpy as np
import pandas as pd
import vigra

from cluster_tools.morphology import MorphologyWorkflow
from elf.io import open_file
from skimage.measure import regionprops
from .utils import remove_background_label_row, read_table
from ..utils import write_global_config
from ..validation.tables import get_columns_for_table_format


def check_and_copy_default_table(input_path, output_path, is_2d, suppress_warnings=False):
    tab = read_table(input_path)
    required_column_names, recommended_column_names, _ = get_columns_for_table_format(tab, is_2d)
    missing_columns = list(required_column_names - set(tab.columns))
    if missing_columns:
        raise ValueError(f"The table at {input_path} is missing the following required columns: {missing_columns}")
    missing_columns = list(recommended_column_names - set(tab.columns))
    if missing_columns and not suppress_warnings:
        warnings.warn(f"The table at {input_path} is missing the following recommended columns: {missing_columns}")
    tab.to_csv(output_path, sep="\t", index=False, na_rep="nan")


def _compute_table_2d(seg_path, seg_key, resolution):
    ndim = 2
    with open_file(seg_path, "r") as f:
        seg = f[seg_key][:]
    assert len(resolution) == seg.ndim == ndim

    centers = vigra.filters.eccentricityCenters(seg.astype("uint32"))
    props = regionprops(seg)
    tab = np.array([
        [p.label]
        + [ce * res for ce, res in zip(centers[p.label], resolution)]
        + [float(bb) * res for bb, res in zip(p.bbox[:ndim], resolution)]
        + [float(bb) * res for bb, res in zip(p.bbox[ndim:], resolution)]
        + [p.area]
        for p in props
    ])

    col_names = ["label_id", "anchor_y", "anchor_x",
                 "bb_min_y", "bb_min_x", "bb_max_y", "bb_max_x", "n_pixels"]
    assert tab.shape[1] == len(col_names), f"{tab.shape}, {len(col_names)}"
    return pd.DataFrame(tab, columns=col_names)


def _table_impl(input_path, input_key, tmp_folder, target, max_jobs):
    task = MorphologyWorkflow

    out_path = os.path.join(tmp_folder, "data.n5")
    config_folder = os.path.join(tmp_folder, "configs")

    out_key = "attributes"
    t = task(tmp_folder=tmp_folder, max_jobs=max_jobs, target=target,
             config_dir=config_folder,
             input_path=input_path, input_key=input_key,
             output_path=out_path, output_key=out_key,
             prefix="attributes", max_jobs_merge=min(32, max_jobs))
    ret = luigi.build([t], local_scheduler=True)
    if not ret:
        raise RuntimeError("Attribute workflow failed")
    return out_path, out_key


def _n5_to_pandas(input_path, input_key, resolution, anchors):
    # load the attributes from n5
    with open_file(input_path, "r") as f:
        attributes = f[input_key][:]
    label_ids = attributes[:, 0:1]

    # the colomn names
    col_names = ["label_id",
                 "anchor_x", "anchor_y", "anchor_z",
                 "bb_min_x", "bb_min_y", "bb_min_z",
                 "bb_max_x", "bb_max_y", "bb_max_z",
                 "n_pixels"]

    # we need to switch from our axis conventions (zyx)
    # to java conventions (xyz)
    res_in_micron = resolution[::-1]

    def translate_coordinate_tuple(coords):
        coords = coords[:, ::-1]
        for d in range(3):
            coords[:, d] *= res_in_micron[d]
        return coords

    # center of mass / anchor points
    com = attributes[:, 2:5]
    if anchors is None:
        anchors = translate_coordinate_tuple(com)
    else:
        assert len(anchors) == len(com)
        assert anchors.shape[1] == 3

        # some of the corrected anchors might not be present,
        # so we merge them with the com here
        invalid_anchors = np.isclose(anchors, 0.).all(axis=1)
        anchors[invalid_anchors] = com[invalid_anchors]
        anchors = translate_coordinate_tuple(anchors)

    # attributes[5:8] = min coordinate of bounding box
    minc = translate_coordinate_tuple(attributes[:, 5:8])
    # attributes[8:11] = min coordinate of bounding box
    maxc = translate_coordinate_tuple(attributes[:, 8:11])

    # NOTE attributes[1] = size in pixel
    # wrie the output table
    data = np.concatenate([label_ids, anchors, minc, maxc, attributes[:, 1:2]], axis=1)
    df = pd.DataFrame(data, columns=col_names)
    df = remove_background_label_row(df)
    return df


def _compute_table_3d(seg_path, seg_key, resolution, correct_anchors, tmp_folder, target, max_jobs):
    # prepare cluster tools tasks
    write_global_config(os.path.join(tmp_folder, "configs"))
    # make base attributes as n5 dataset
    tmp_path, tmp_key = _table_impl(seg_path, seg_key, tmp_folder, target, max_jobs)
    # TODO implement scalable anchor correction via distance transform maxima
    # correct anchor positions
    if correct_anchors:
        raise NotImplementedError("Anchor correction is not implemented yet")
        # anchors = anchor_correction(seg_path, seg_key, tmp_folder, target, max_jobs)
    else:
        anchors = None
    return _n5_to_pandas(tmp_path, tmp_key, resolution, anchors)


def _remove_empty_columns(table):
    table = table[table.n_pixels != 0]
    return table


def compute_default_table(seg_path, seg_key, table_path,
                          resolution, tmp_folder, target, max_jobs,
                          correct_anchors=False):
    """ Compute the default table for the input segmentation, consisting of the
    attributes necessary to enable tables in the mobie-fiji-viewer.

    Arguments:
        seg_path [str] - input path to the segmentation
        seg_key [str] - key to the segmenation
        table_path [str] - path to the output table
        resolution [list[float]] - resolution of the data in microns
        tmp_folder [str] - folder for temporary files
        target [str] - computation target
        max_jobs [int] - number of jobs
        correct_anchors [bool] - whether to move the anchor points into segmentation objects.
            Anchor points may be outside of objects in case of concave objects. (default: False)
    """

    with open_file(seg_path, "r") as f:
        ndim = f[seg_key].ndim

    if ndim == 2:
        table = _compute_table_2d(seg_path, seg_key, resolution)
    else:
        table = _compute_table_3d(seg_path, seg_key, resolution, correct_anchors, tmp_folder, target, max_jobs)

    table = _remove_empty_columns(table)

    # write output to csv
    table_folder = os.path.split(table_path)[0]
    os.makedirs(table_folder, exist_ok=True)
    table.to_csv(table_path, sep="\t", index=False, na_rep="nan")
