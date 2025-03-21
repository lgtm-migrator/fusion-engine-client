#!/usr/bin/env python3

from typing import Tuple, Union, List, Any

from collections import namedtuple
import logging
import os
import sys
import webbrowser

from gpstime import gpstime
import plotly
import plotly.graph_objs as go
from plotly.subplots import make_subplots
from pymap3d import geodetic2ecef

# If running as a script, add fusion-engine-client/ to the Python import path and correct __package__ to enable relative
# imports.
if __name__ == "__main__" and (__package__ is None or __package__ == ''):
    root_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../..'))
    sys.path.append(root_dir)
    __package__ = "fusion_engine_client.analysis"

from ..messages import *
from .attitude import get_enu_rotation_matrix
from .file_reader import FileReader
from ..utils import trace
from ..utils.argument_parser import ArgumentParser
from ..utils.log import locate_log, DEFAULT_LOG_BASE_DIR
_logger = logging.getLogger('point_one.fusion_engine.analysis.analyzer')


SolutionTypeInfo = namedtuple('SolutionTypeInfo', ['name', 'style'])

_SOLUTION_TYPE_MAP = {
    SolutionType.Invalid: SolutionTypeInfo(name='Invalid', style={'color': 'black'}),
    SolutionType.Integrate: SolutionTypeInfo(name='Integrated', style={'color': 'cyan'}),
    SolutionType.AutonomousGPS: SolutionTypeInfo(name='Standalone', style={'color': 'red'}),
    SolutionType.DGPS: SolutionTypeInfo(name='DGPS', style={'color': 'blue'}),
    SolutionType.RTKFloat: SolutionTypeInfo(name='RTK Float', style={'color': 'green'}),
    SolutionType.RTKFixed: SolutionTypeInfo(name='RTK Fixed', style={'color': 'orange'}),
    SolutionType.PPP: SolutionTypeInfo(name='PPP', style={'color': 'pink'}),
    SolutionType.Visual: SolutionTypeInfo(name='Vision', style={'color': 'purple'}),
}


def _data_to_table(col_titles: List[str], col_values: List[List[Any]]):
    table_html = '<table><tr style="background-color: #a2c4fa">'
    for title in col_titles:
        table_html += f'<th>{title}</th>'
    table_html += '</tr>'
    num_rows = min([len(l) for l in col_values])
    for row_idx in range(num_rows):
        table_html += '<tr>'

        separator_row = col_values[0][row_idx] is None
        for col_data in col_values:
            if separator_row:
                table_html += '<td><hr></td>'
            else:
                table_html += f'<td>{col_data[row_idx]}</td>'

        table_html += '</tr>'
    table_html += '</table>'
    return table_html


_page_template = '''\
<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html>
<head>
  <meta content="text/html; charset=ISO-8859-1" http-equiv="content-type">
  <title>%(title)s</title>
</head>
<body>
  %(body)s
</body>
</html>
'''


class Analyzer(object):
    logger = _logger

    def __init__(self, file: Union[FileReader, str], output_dir: str = None, ignore_index: bool = False,
                 prefix: str = '',
                 time_range: Tuple[Union[float, Timestamp], Union[float, Timestamp]] = None,
                 absolute_time: bool = False,
                 max_messages: int = None):
        """!
        @brief Create an analyzer for the specified log.

        @param file A @ref FileReader instance, or the path to a file to be loaded.
        @param output_dir The directory where output will be stored.
        @param ignore_index If `True`, do not use the `.p1i` index file if present, and instead regenerate it from the
               `.p1log` data file.
        @param prefix An optional prefix to be appended to the generated filenames.
        @param time_range An optional length-2 tuple specifying desired start and end bounds on the data timestamps.
               Both the start and end values may be set to `None` to read all data.
        @param absolute_time If `True`, interpret the timestamps in `time_range` as absolute P1 times. Otherwise, treat
               them as relative to the first message in the file.
        @param max_messages If set, read up to the specified maximum number of messages. Applies across all message
               types.
        """
        if isinstance(file, str):
            self.reader = FileReader(file, regenerate_index=ignore_index)
        else:
            self.reader = file

        self.output_dir = output_dir
        self.prefix = prefix

        self.params = {
            'time_range': time_range if time_range is not None else (None, None),
            'absolute_time': absolute_time,
            'max_messages': max_messages,
            'show_progress': True,
            'return_numpy': True
        }

        self.t0 = self.reader.t0
        self.system_t0 = self.reader.get_system_t0()

        self.plots = {}
        self.summary = ''

        self._mapbox_token_missing = False

        if self.output_dir is not None:
            if not os.path.exists(self.output_dir):
                os.makedirs(self.output_dir)

    def plot_time_scale(self):
        if self.output_dir is None:
            return

        # Setup the figure.
        figure = make_subplots(rows=2, cols=1, print_grid=False, shared_xaxes=True,
                               subplot_titles=['Device Time vs Relative Time', 'Delta-Time'])

        figure['layout'].update(showlegend=False)
        for i in range(2):
            figure['layout']['xaxis%d' % (i + 1)].update(title="Relative Time (sec)", showticklabels=True)
        figure['layout']['yaxis1'].update(title="Absolute Time",
                                          ticktext=['P1/GPS Time', 'System Time'],
                                          tickvals=[1, 2])
        figure['layout']['yaxis2'].update(title="Delta-Time (sec)", rangemode="tozero")

        # Read the pose data to get P1 and GPS timestamps.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]

        if len(pose_data.p1_time) > 0:
            time = pose_data.p1_time - float(self.t0)

            dp1_time = np.diff(time, prepend=np.nan)
            dp1_time = np.round(dp1_time * 1e3) * 1e-3

            # plotly starts to struggle with > 2 hours of data and won't display mouseover text, so decimate if
            # necessary.
            dt_sec = time[-1] - time[0]
            if dt_sec > 7200.0:
                step = math.ceil(dt_sec / 7200.0)
                idx = np.full_like(time, False, dtype=bool)
                idx[0::step] = True

                time = time[idx]
                p1_time = pose_data.p1_time[idx]
                dp1_time = dp1_time[idx]
                gps_time = pose_data.gps_time[idx]

                figure.layout.annotations[0].text += "<br>Decimated %dx" % step
            else:
                p1_time = pose_data.p1_time
                gps_time = pose_data.gps_time

            def gps_sec_to_string(gps_time_sec):
                if np.isnan(gps_time_sec):
                    return "GPS: N/A<br>UTC: N/A"
                else:
                    SECS_PER_WEEK = 7 * 24 * 3600.0
                    week = int(gps_time_sec / SECS_PER_WEEK)
                    tow_sec = gps_time_sec - week * SECS_PER_WEEK
                    utc_time = gpstime.fromgps(gps_time_sec)
                    return "GPS: %d:%.3f (%.3f sec)<br>UTC: %s" %\
                           (week, tow_sec, gps_time_sec, utc_time.strftime('%Y-%m-%d %H:%M:%S %Z'))

            text = ['P1: %.3f sec<br>%s' % (p, gps_sec_to_string(g)) for p, g in zip(p1_time, gps_time)]
            figure.add_trace(go.Scattergl(x=time, y=np.full_like(time, 1), name='P1/GPS Time', text=text,
                                          mode='markers'),
                             1, 1)

            figure.add_trace(go.Scattergl(x=time, y=dp1_time, name='P1/GPS Time', text=text,
                                          mode='markers'),
                             2, 1)

        # Read system timestamps from event notifications, if present.
        result = self.reader.read(message_types=[EventNotificationMessage], **self.params)
        event_data = result[EventNotificationMessage.MESSAGE_TYPE]

        system_time_sec = None
        if len(event_data.messages) > 0:
            system_time_sec = np.array([(m.system_time_ns * 1e-9) for m in event_data.messages])

        if system_time_sec is not None:
            time = system_time_sec - self.system_t0

            # plotly starts to struggle with > 2 hours of data and won't display mouseover text, so decimate if
            # necessary.
            dt_sec = time[-1] - time[0]
            if dt_sec > 7200.0:
                step = math.ceil(dt_sec / 7200.0)
                idx = np.full_like(time, False, dtype=bool)
                idx[0::step] = True

                time = time[idx]
                system_time_sec = system_time_sec[idx]

            text = ['System: %.3f sec' % t for t in system_time_sec]
            figure.add_trace(go.Scattergl(x=time, y=np.full_like(time, 2), name='System Time', text=text,
                                          mode='markers'),
                             1, 1)

        self._add_figure(name="time_scale", figure=figure, title="Time Scale")

    def plot_pose(self):
        """!
        @brief Plot position/attitude solution data.
        """
        if self.output_dir is None:
            return

        # Read the pose data.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]

        if len(pose_data.p1_time) == 0:
            self.logger.info('No pose data available. Skipping pose vs time plot.')
            return

        time = pose_data.p1_time - float(self.t0)

        valid_idx = np.logical_and(~np.isnan(pose_data.p1_time), pose_data.solution_type != SolutionType.Invalid)
        if not np.any(valid_idx):
            self.logger.info('No valid position solutions detected.')
            return

        first_idx = np.argmax(valid_idx)
        c_enu_ecef = get_enu_rotation_matrix(*pose_data.lla_deg[0:2, first_idx], deg=True)

        # Setup the figure.
        figure = make_subplots(rows=2, cols=3, print_grid=False, shared_xaxes=True,
                               subplot_titles=['Attitude (YPR)', 'ENU Displacement', 'Body Velocity',
                                               'Attitude Std', 'ENU Position Std', 'Velocity Std'])

        figure['layout'].update(showlegend=True)
        for i in range(6):
            figure['layout']['xaxis%d' % (i + 1)].update(title="Time (sec)", showticklabels=True, matches='x')
        figure['layout']['yaxis1'].update(title="Degrees")
        figure['layout']['yaxis2'].update(title="Meters")
        figure['layout']['yaxis3'].update(title="Meters/Second")
        figure['layout']['yaxis4'].update(title="Degrees")
        figure['layout']['yaxis5'].update(title="Meters")
        figure['layout']['yaxis6'].update(title="Meters/Second")

        # Plot YPR.
        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_deg[0, :], name='Yaw', legendgroup='yaw',
                                      mode='lines', line={'color': 'red'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_deg[1, :], name='Pitch', legendgroup='pitch',
                                      mode='lines', line={'color': 'green'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_deg[2, :], name='Roll', legendgroup='roll',
                                      mode='lines', line={'color': 'blue'}),
                         1, 1)

        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_std_deg[0, :], name='Yaw', legendgroup='yaw',
                                      showlegend=False, mode='lines', line={'color': 'red'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_std_deg[1, :], name='Pitch', legendgroup='pitch',
                                      showlegend=False, mode='lines', line={'color': 'green'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.ypr_std_deg[2, :], name='Roll', legendgroup='roll',
                                      showlegend=False, mode='lines', line={'color': 'blue'}),
                         2, 1)

        # Plot position/displacement.
        position_ecef_m = np.array(geodetic2ecef(lat=pose_data.lla_deg[0, :], lon=pose_data.lla_deg[1, :],
                                                 alt=pose_data.lla_deg[0, :], deg=True))
        displacement_ecef_m = position_ecef_m - position_ecef_m[:, first_idx].reshape(3, 1)
        displacement_enu_m = c_enu_ecef.dot(displacement_ecef_m)
        figure.add_trace(go.Scattergl(x=time, y=displacement_enu_m[0, :], name='East', legendgroup='e',
                                      mode='lines', line={'color': 'red'}),
                         1, 2)
        figure.add_trace(go.Scattergl(x=time, y=displacement_enu_m[1, :], name='North', legendgroup='n',
                                      mode='lines', line={'color': 'green'}),
                         1, 2)
        figure.add_trace(go.Scattergl(x=time, y=displacement_enu_m[2, :], name='Up', legendgroup='u',
                                      mode='lines', line={'color': 'blue'}),
                         1, 2)

        figure.add_trace(go.Scattergl(x=time, y=pose_data.position_std_enu_m[0, :], name='East', legendgroup='e',
                                      showlegend=False, mode='lines', line={'color': 'red'}),
                         2, 2)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.position_std_enu_m[1, :], name='North', legendgroup='n',
                                      showlegend=False, mode='lines', line={'color': 'green'}),
                         2, 2)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.position_std_enu_m[2, :], name='Up', legendgroup='u',
                                      showlegend=False, mode='lines', line={'color': 'blue'}),
                         2, 2)

        # Plot velocity.
        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_body_mps[0, :], name='X', legendgroup='x',
                                      mode='lines', line={'color': 'red'}),
                         1, 3)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_body_mps[1, :], name='Y', legendgroup='y',
                                      mode='lines', line={'color': 'green'}),
                         1, 3)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_body_mps[2, :], name='Z', legendgroup='z',
                                      mode='lines', line={'color': 'blue'}),
                         1, 3)

        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_std_body_mps[0, :], name='X', legendgroup='x',
                                      showlegend=False, mode='lines', line={'color': 'red'}),
                         2, 3)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_std_body_mps[1, :], name='Y', legendgroup='y',
                                      showlegend=False, mode='lines', line={'color': 'green'}),
                         2, 3)
        figure.add_trace(go.Scattergl(x=time, y=pose_data.velocity_std_body_mps[2, :], name='Z', legendgroup='z',
                                      showlegend=False, mode='lines', line={'color': 'blue'}),
                         2, 3)

        self._add_figure(name="pose", figure=figure, title="Vehicle Pose vs. Time")

    def plot_calibration(self):
        """!
        @brief Plot the calibration progress over time.
        """
        if self.output_dir is None:
            return

        # Read the pose data.
        result = self.reader.read(message_types=[CalibrationStatus], **self.params)
        cal_data = result[CalibrationStatus.MESSAGE_TYPE]

        if len(cal_data.p1_time) == 0:
            self.logger.info('No calibration data available. Skipping calibration plot.')
            return

        time = cal_data.p1_time - float(self.t0)
        text = ["Time: %.3f sec (%.3f sec)" % (t, t + float(self.t0)) for t in time]

        # Map calibration stage enum values onto a [0, N) range for plotting.
        stage_map = {e.value: i for i, e in enumerate(CalibrationStage)}
        calibration_stage = [stage_map[s] for s in cal_data.calibration_stage]

        # Setup the figure.
        figure = make_subplots(rows=4, cols=1, print_grid=False, shared_xaxes=True,
                               subplot_titles=['<- Percent Complete // Stage ->', 'Mounting Angles',
                                               'Mounting Angle Standard Deviation', 'Travel Distance'],
                               specs=[[{"secondary_y": True}], [{}], [{}], [{}]])

        figure['layout'].update(showlegend=True)
        for i in range(4):
            figure['layout']['xaxis%d' % (i + 1)].update(title="Time (sec)", showticklabels=True)
        figure['layout']['yaxis1'].update(title="Percent Complete", range=[0, 100])
        figure['layout']['yaxis2'].update(ticktext=['%s' % e.name for e in CalibrationStage],
                                          tickvals=list(range(len(stage_map))))
        figure['layout']['yaxis3'].update(title="Degrees")
        figure['layout']['yaxis4'].update(title="Degrees")
        figure['layout']['yaxis5'].update(title="Meters")

        # Plot calibration stage and completion percentages.
        figure.add_trace(go.Scattergl(x=time, y=cal_data.gyro_bias_percent_complete, name='Gyro Bias Completion',
                                      text=text, mode='lines', line={'color': 'red'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.accel_bias_percent_complete, name='Accel Bias Completion',
                                      text=text, mode='lines', line={'color': 'green'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.mounting_angle_percent_complete,
                                      name='Mounting Angle Completion', text=text,
                                      mode='lines', line={'color': 'blue'}),
                         1, 1)

        figure.add_trace(go.Scattergl(x=time, y=calibration_stage, name='Stage', text=text,
                                      mode='lines', line={'color': 'black', 'dash': 'dash'}),
                         1, 1, secondary_y=True)

        # Plot mounting angles.
        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_deg[0, :], name='Yaw', legendgroup='y', text=text,
                                      mode='lines', line={'color': 'red'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_deg[1, :], name='Pitch', legendgroup='p', text=text,
                                      mode='lines', line={'color': 'green'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_deg[2, :], name='Roll', legendgroup='r', text=text,
                                      mode='lines', line={'color': 'blue'}),
                         2, 1)

        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_std_dev_deg[0, :], name='Yaw Std Dev', legendgroup='y',
                                      text=text, mode='lines', line={'color': 'red'}),
                         3, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_std_dev_deg[1, :], name='Pitch Std Dev', legendgroup='p',
                                      text=text, mode='lines', line={'color': 'green'}),
                         3, 1)
        figure.add_trace(go.Scattergl(x=time, y=cal_data.ypr_std_dev_deg[2, :], name='Roll Std Dev', legendgroup='r',
                                      text=text, mode='lines', line={'color': 'blue'}),
                         3, 1)

        thresh_time = time[np.array((0, -1))]
        figure.add_trace(go.Scattergl(x=thresh_time, y=[cal_data.mounting_angle_max_std_dev_deg[0]] * 2,
                                      name='Max Yaw Std Dev', legendgroup='y',
                                      mode='lines', line={'color': 'red', 'dash': 'dash'}),
                         3, 1)
        figure.add_trace(go.Scattergl(x=thresh_time, y=[cal_data.mounting_angle_max_std_dev_deg[1]] * 2,
                                      name='Max Pitch Std Dev', legendgroup='p',
                                      text=text, mode='lines', line={'color': 'green', 'dash': 'dash'}),
                         3, 1)
        figure.add_trace(go.Scattergl(x=thresh_time, y=[cal_data.mounting_angle_max_std_dev_deg[2]] * 2,
                                      name='Max Roll Std Dev', legendgroup='r',
                                      text=text, mode='lines', line={'color': 'blue', 'dash': 'dash'}),
                         3, 1)

        # Plot travel distance.
        figure.add_trace(go.Scattergl(x=time, y=cal_data.travel_distance_m, name='Travel Distance', text=text,
                                      mode='lines', line={'color': 'blue'}),
                         4, 1)
        figure.add_trace(go.Scattergl(x=thresh_time, y=[cal_data.min_travel_distance_m] * 2,
                                      name='Min Travel Distance', text=text,
                                      mode='lines', line={'color': 'black', 'dash': 'dash'}),
                         4, 1)

        self._add_figure(name="calibration", figure=figure, title="Calibration Status")

    def plot_solution_type(self):
        """!
        @brief Plot the solution type over time.
        """
        if self.output_dir is None:
            return

        # Read the pose data.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]

        if len(pose_data.p1_time) == 0:
            self.logger.info('No pose data available. Skipping solution type plot.')
            return

        # Setup the figure.
        figure = make_subplots(rows=1, cols=1, print_grid=False, shared_xaxes=True, subplot_titles=['Solution Type'])

        figure['layout']['xaxis'].update(title="Time (sec)")
        figure['layout']['yaxis1'].update(title="Solution Type",
                                          ticktext=['%s (%d)' % (e.name, e.value) for e in SolutionType],
                                          tickvals=[e.value for e in SolutionType])

        time = pose_data.p1_time - float(self.t0)

        text = ["Time: %.3f sec (%.3f sec)" % (t, t + float(self.t0)) for t in time]
        figure.add_trace(go.Scattergl(x=time, y=pose_data.solution_type, text=text, mode='markers'), 1, 1)

        self._add_figure(name="solution_type", figure=figure, title="Solution Type")

    def _plot_displacement(self, source, time, solution_type, displacement_enu_m, std_enu_m):
        """!
        @brief Generate a topocentric (top-down) plot of position displacement, as well as plot of displacement over
               time.
        """
        if self.output_dir is None:
            return

        # Setup the figure.
        topo_figure = make_subplots(rows=1, cols=1, print_grid=False, shared_xaxes=False,
                                    subplot_titles=['Displacement'])
        topo_figure['layout']['xaxis1'].update(title="East (m)")
        topo_figure['layout']['yaxis1'].update(title="North (m)")

        time_figure = make_subplots(rows=4, cols=1, print_grid=False, shared_xaxes=True,
                                    subplot_titles=['3D', 'North', 'East', 'Up'])
        time_figure['layout'].update(showlegend=True)
        for i in range(4):
            time_figure['layout']['xaxis%d' % (i + 1)].update(title="Time (sec)", showticklabels=True)
        time_figure['layout']['yaxis1'].update(title="Displacement (m)")
        time_figure['layout']['yaxis2'].update(title="Displacement (m)")
        time_figure['layout']['yaxis3'].update(title="Displacement (m)")
        time_figure['layout']['yaxis4'].update(title="Displacement (m)")

        # Remove invalid solutions.
        valid_idx = np.logical_and(~np.isnan(time), solution_type != SolutionType.Invalid)
        if not np.any(valid_idx):
            self.logger.info('No valid position solutions detected. Skipping displacement plots.')
            return

        # Add statistics to the figure title.
        format = 'Mean: %(mean).2f m, Median: %(median).2f m, Min: %(min).2f m, Max: %(max).2f m, Std Dev: %(std).2f m'
        displacement_3d_m = np.linalg.norm(displacement_enu_m, axis=0)
        extra_text = '[All] ' + format % {
            'mean': np.mean(displacement_3d_m),
            'median': np.median(displacement_3d_m),
            'min': np.min(displacement_3d_m),
            'max': np.max(displacement_3d_m),
            'std': np.std(displacement_3d_m),
        }

        idx = solution_type == SolutionType.RTKFixed
        if np.any(idx):
            displacement_3d_m = np.linalg.norm(displacement_enu_m[:, idx], axis=0)
            extra_text += '<br>[Fixed] ' + format % {
                'mean': np.mean(displacement_3d_m),
                'median': np.median(displacement_3d_m),
                'min': np.min(displacement_3d_m),
                'max': np.max(displacement_3d_m),
                'std': np.std(displacement_3d_m),
            }

        topo_figure.update_layout(title_text=extra_text)
        time_figure.update_layout(title_text=extra_text)

        # Plot the data.
        def _plot_data(name, idx, marker_style=None):
            style = {'mode': 'markers', 'marker': {'size': 8}, 'showlegend': True, 'legendgroup': name}
            if marker_style is not None:
                style['marker'].update(marker_style)

            if np.any(idx):
                text = ["Time: %.3f sec (%.3f sec)<br>Delta (ENU): (%.2f, %.2f, %.2f) m" \
                        "<br>Std (ENU): (%.2f, %.2f, %.2f) m" %
                        (t, t + float(self.t0), *delta, *std)
                        for t, delta, std in zip(time[idx], displacement_enu_m[:, idx].T, std_enu_m[:, idx].T)]
                topo_figure.add_trace(go.Scattergl(x=displacement_enu_m[0, idx], y=displacement_enu_m[1, idx],
                                                   name=name, text=text, **style), 1, 1)

                time_figure.add_trace(go.Scattergl(x=time[idx], y=np.linalg.norm(displacement_enu_m[:, idx], axis=0),
                                                   name=name, text=text, **style), 1, 1)
                style['showlegend'] = False
                time_figure.add_trace(go.Scattergl(x=time[idx], y=displacement_enu_m[0, idx], name=name,
                                                   text=text, **style), 2, 1)
                time_figure.add_trace(go.Scattergl(x=time[idx], y=displacement_enu_m[1, idx], name=name,
                                                   text=text, **style), 3, 1)
                time_figure.add_trace(go.Scattergl(x=time[idx], y=displacement_enu_m[2, idx], name=name,
                                                   text=text, **style), 4, 1)
            else:
                # If there's no data, draw a dummy trace so it shows up in the legend anyway.
                topo_figure.add_trace(go.Scattergl(x=[np.nan], y=[np.nan], name=name, visible='legendonly', **style),
                                      1, 1)
                time_figure.add_trace(go.Scattergl(x=[np.nan], y=[np.nan], name=name, visible='legendonly', **style),
                                      1, 1)

        for type, info in _SOLUTION_TYPE_MAP.items():
            _plot_data(info.name, solution_type == type, marker_style=info.style)

        name = source.replace(' ', '_').lower()
        self._add_figure(name=f"{name}_top_down", figure=topo_figure, title=f"{source}: Top-Down (Topocentric)")
        self._add_figure(name=f"{name}_displacement", figure=time_figure, title=f"{source}: vs. Time")

    def plot_pose_displacement(self):
        """!
        @brief Generate a topocentric (top-down) plot of position displacement, as well as plot of displacement over
               time.
        """
        if self.output_dir is None:
            return

        # Read the pose data.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]

        if len(pose_data.p1_time) == 0:
            self.logger.info('No pose data available. Skipping displacement plots.')
            return

        # Remove invalid solutions.
        valid_idx = np.logical_and(~np.isnan(pose_data.p1_time), pose_data.solution_type != SolutionType.Invalid)
        if not np.any(valid_idx):
            self.logger.info('No valid position solutions detected. Skipping displacement plots.')
            return

        time = pose_data.p1_time[valid_idx] - float(self.t0)
        solution_type = pose_data.solution_type[valid_idx]
        lla_deg = pose_data.lla_deg[:, valid_idx]
        std_enu_m = pose_data.position_std_enu_m[:, valid_idx]

        # Convert to ENU displacement with respect to the median position (we use median instead of centroid just in
        # case there are one or two huge outliers).
        position_ecef_m = np.array(geodetic2ecef(lat=lla_deg[0, :], lon=lla_deg[1, :], alt=lla_deg[2, :], deg=True))
        center_ecef_m = np.median(position_ecef_m, axis=1)
        displacement_ecef_m = position_ecef_m - center_ecef_m.reshape(3, 1)
        c_enu_ecef = get_enu_rotation_matrix(*lla_deg[0:2, 0], deg=True)
        displacement_enu_m = c_enu_ecef.dot(displacement_ecef_m)

        self._plot_displacement('Pose Displacement', time, solution_type, displacement_enu_m, std_enu_m)

    def plot_relative_position_to_base_station(self):
        """!
        @brief Generate a topocentric (top-down) plot of relative position vs base station, as well as plot of relative
               position over time.
        """
        if self.output_dir is None:
            return

        # Read the pose data.
        result = self.reader.read(message_types=[RelativeENUPositionMessage], **self.params)
        relative_position_data = result[RelativeENUPositionMessage.MESSAGE_TYPE]

        if len(relative_position_data.p1_time) == 0:
            self.logger.info('No relative ENU data available. Skipping relative position vs base station plots.')
            return

        # Remove invalid solutions.
        valid_idx = ~np.isnan(relative_position_data.relative_position_enu_m[0, :])

        if not np.any(valid_idx):
            self.logger.info('No valid position solutions detected. Skipping relative position vs base station plots.')
            return

        time = relative_position_data.p1_time[valid_idx] - float(self.t0)
        solution_type = relative_position_data.solution_type[valid_idx]
        displacement_enu_m = relative_position_data.relative_position_enu_m[:, valid_idx]
        std_enu_m = relative_position_data.position_std_enu_m[:, valid_idx]

        self._plot_displacement('Relative Position vs Base Station', time, solution_type, displacement_enu_m, std_enu_m)

    def plot_map(self, mapbox_token):
        """!
        @brief Plot a map of the position data.
        """
        if self.output_dir is None:
            return

        mapbox_token = self.get_mapbox_token(mapbox_token)
        if mapbox_token is None or mapbox_token == "":
            self.logger.info('*' * 80 + '\n\n' +
                             'Mapbox token not specified. Disabling satellite imagery. For satellite imagery,\n'
                             'please provide a Mapbox token using --mapbox-token or by setting the\n'
                             'MAPBOX_ACCESS_TOKEN environment variable.' +
                             '\n\n' + '*' * 80)
            self._mapbox_token_missing = True
            mapbox_token = None

        # Read the pose data.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]

        if len(pose_data.p1_time) == 0:
            self.logger.info('No pose data available. Skipping map display.')
            return

        # Remove invalid solutions.
        valid_idx = np.logical_and(~np.isnan(pose_data.p1_time), pose_data.solution_type != SolutionType.Invalid)
        if not np.any(valid_idx):
            self.logger.info('No valid position solutions detected.')
            return

        time = pose_data.p1_time[valid_idx] - float(self.t0)
        solution_type = pose_data.solution_type[valid_idx]
        lla_deg = pose_data.lla_deg[:, valid_idx]
        std_enu_m = pose_data.position_std_enu_m[:, valid_idx]

        # Add data to the map.
        map_data = []

        def _plot_data(name, idx, marker_style=None):
            style = {'mode': 'markers', 'marker': {'size': 8}, 'showlegend': True}
            if marker_style is not None:
                style['marker'].update(marker_style)

            if np.any(idx):
                text = ["Time: %.3f sec (%.3f sec)<br>Std (ENU): (%.2f, %.2f, %.2f) m" %
                        (t, t + float(self.t0), std[0], std[1], std[2])
                        for t, std in zip(time[idx], std_enu_m[:, idx].T)]
                map_data.append(go.Scattermapbox(lat=lla_deg[0, idx], lon=lla_deg[1, idx], name=name, text=text,
                                                 **style))
            else:
                # If there's no data, draw a dummy trace so it shows up in the legend anyway.
                map_data.append(go.Scattermapbox(lat=[np.nan], lon=[np.nan], name=name, visible='legendonly', **style))

        for type, info in _SOLUTION_TYPE_MAP.items():
            _plot_data(info.name, solution_type == type, marker_style=info.style)

        # Create the map.
        title = 'Vehicle Trajectory'
        if mapbox_token is None:
            title += '<br>For satellite imagery, please provide a Mapbox token using --mapbox-token or by setting ' \
                     'MAPBOX_ACCESS_TOKEN.'

        layout = go.Layout(
            autosize=True,
            hovermode='closest',
            title=title,
            mapbox=dict(
                accesstoken=mapbox_token,
                bearing=0,
                center=dict(
                    lat=lla_deg[0, 0],
                    lon=lla_deg[1, 0],
                ),
                pitch=0,
                zoom=18,
                style='open-street-map' if mapbox_token is None else 'satellite-streets',
            ),
        )

        figure = go.Figure(data=map_data, layout=layout)
        figure['layout'].update(showlegend=True)

        self._add_figure(name="map", figure=figure, title="Vehicle Trajectory (Map)")

    def plot_wheel_data(self):
        """!
        @brief Plot wheel tick/speed data.
        """
        if self.output_dir is None:
            return

        self._plot_wheel_ticks_or_speeds(type='speed')
        self._plot_wheel_ticks_or_speeds(type='tick')

    def _plot_wheel_ticks_or_speeds(self, type):
        """!
        @brief Plot wheel speed or tick data.
        """
        # Read the data.
        if type == 'tick':
            wheel_measurement_type = WheelTickMeasurement
            vehicle_measurement_type = VehicleTickMeasurement
        else:
            wheel_measurement_type = WheelSpeedMeasurement
            vehicle_measurement_type = VehicleSpeedMeasurement

        result = self.reader.read(message_types=[wheel_measurement_type, vehicle_measurement_type],
                                  remove_nan_times=False, **self.params)

        wheel_data = result[wheel_measurement_type.MESSAGE_TYPE]
        if len(wheel_data.p1_time) == 0:
            wheel_data = None

        vehicle_data = result[vehicle_measurement_type.MESSAGE_TYPE]
        if len(vehicle_data.p1_time) == 0:
            vehicle_data = None

        if wheel_data is None and vehicle_data is None:
            self.logger.info('No wheel %s data available. Skipping plot.' % type)
            return

        # Setup the figure.
        if type == 'tick':
            titles = ['Tick Count', 'Tick Rate', 'Gear/Direction']
        else:
            titles = ['Speed', 'Gear/Direction']

        figure = make_subplots(rows=len(titles), cols=1, print_grid=False, shared_xaxes=True, subplot_titles=titles)

        figure['layout'].update(showlegend=True, modebar_add=['v1hovermode', 'toggleSpikelines'])
        for i in range(len(titles)):
            figure['layout']['xaxis%d' % (i + 1)].update(title="Time (sec)", showticklabels=True)

        if type == 'tick':
            figure['layout']['yaxis1'].update(title="Tick Count")
            figure['layout']['yaxis2'].update(title="Tick Rate (ticks/s)")
        else:
            figure['layout']['yaxis1'].update(title="Speed (m/s)")

        figure['layout']['yaxis%d' % (len(titles))].update(title="Gear/Direction",
                                                           ticktext=['%s (%d)' % (e.name, e.value) for e in GearType],
                                                           tickvals=[e.value for e in GearType])

        # Check if the data has P1 time available. If not, we'll plot in the original source time.
        wheel_time_source = None
        vehicle_time_source = None

        if wheel_data is not None:
            if np.all(np.isnan(wheel_data.p1_time)):
                if np.any(np.diff(wheel_data.measurement_time_source) != 0):
                    self.logger.warning('Detected multiple time source types in wheel %s data.' % type)

                wheel_time_source = SystemTimeSource(wheel_data.measurement_time_source[0])
                self.logger.warning('Wheel %s data does not have P1 time available. Plotting in %s time.' %
                                    (type, self._time_source_to_display_name(wheel_time_source)))
            else:
                wheel_time_source = SystemTimeSource.P1_TIME

            figure['layout']['annotations'][0]['text'] += '<br>Wheel %s Time Source: %s' % \
                                                          (type.title(),
                                                           self._time_source_to_display_name(wheel_time_source))

        if vehicle_data is not None:
            if np.all(np.isnan(vehicle_data.p1_time)):
                if np.any(np.diff(vehicle_data.measurement_time_source) != 0):
                    self.logger.warning('Detected multiple time source types in vehicle %s data.' % type)

                vehicle_time_source = SystemTimeSource(vehicle_data.measurement_time_source[0])
                self.logger.warning('Vehicle %s data does not have P1 time available. Plotting in %s time.' %
                                    (type, self._time_source_to_display_name(vehicle_time_source)))
            else:
                vehicle_time_source = SystemTimeSource.P1_TIME

            figure['layout']['annotations'][0]['text'] += '<br>Vehicle %s Time Source: %s' % \
                                                          (type.title(),
                                                           self._time_source_to_display_name(vehicle_time_source))

        if wheel_time_source is not None and vehicle_time_source is not None:
            if wheel_time_source != vehicle_time_source:
                self.logger.warning('Both wheel and vehicle %s data present, but timestamped with different '
                                    'sources. Plotted data may not align in time.')

        # Plot the data.
        def _plot_trace(time, data, name, color, text):
            if type == 'tick':
                figure.add_trace(go.Scattergl(x=time, y=data, text=text,
                                              name=name, legendgroup=name,
                                              mode='markers', marker={'color': color}),
                                 1, 1)

                dt_sec = np.diff(time)
                ticks_per_sec = np.diff(data) / dt_sec
                figure.add_trace(go.Scattergl(x=time[1:], y=ticks_per_sec, text=text,
                                              name=name, legendgroup=name, showlegend=False,
                                              mode='markers', marker={'color': color}),
                                 2, 1)
            else:
                figure.add_trace(go.Scattergl(x=time, y=data, text=text,
                                              name=name, legendgroup=name,
                                              mode='markers', marker={'color': color}),
                                 1, 1)

        if wheel_data is not None:
            abs_time_sec = self._get_measurement_time(wheel_data, wheel_time_source)
            idx = ~np.isnan(abs_time_sec)
            abs_time_sec = abs_time_sec[idx]

            t0 = self._get_t0_for_time_source(wheel_time_source)
            time = abs_time_sec - t0
            time_name = self._time_source_to_display_name(wheel_time_source)
            text = ["%s Time: %.3f sec" % (time_name, t) for t in abs_time_sec]

            if type == 'tick':
                suffix = 'wheel_ticks'
            else:
                suffix = 'speed_mps'

            _plot_trace(time=time, data=getattr(wheel_data, 'front_left_' + suffix)[idx], text=text,
                        name='Front Left Wheel', color='red')
            _plot_trace(time=time, data=getattr(wheel_data, 'front_right_' + suffix)[idx], text=text,
                        name='Front Right Wheel', color='green')
            _plot_trace(time=time, data=getattr(wheel_data, 'rear_left_' + suffix)[idx], text=text,
                        name='Rear Left Wheel', color='blue')
            _plot_trace(time=time, data=getattr(wheel_data, 'rear_right_' + suffix)[idx], text=text,
                        name='Rear Right Wheel', color='purple')

            figure.add_trace(go.Scattergl(x=time, y=wheel_data.gear[idx], text=text,
                                          name='Gear (Wheel Data)',
                                          mode='markers', marker={'color': 'red'}),
                             3, 1)

        if vehicle_data is not None:
            abs_time_sec = self._get_measurement_time(vehicle_data, vehicle_time_source)
            idx = ~np.isnan(abs_time_sec)
            abs_time_sec = abs_time_sec[idx]

            t0 = self._get_t0_for_time_source(vehicle_time_source)
            time = abs_time_sec - t0
            time_name = self._time_source_to_display_name(vehicle_time_source)
            text = ["%s Time: %.3f sec" % (time_name, t) for t in abs_time_sec]

            if type == 'tick':
                attr = 'tick_count'
            else:
                attr = 'vehicle_speed_mps'

            _plot_trace(time=time, data=getattr(vehicle_data, attr)[idx], text=text,
                        name='Vehicle Data', color='orange')

            figure.add_trace(go.Scattergl(x=time, y=vehicle_data.gear[idx], text=text,
                                          name='Gear (Vehicle Data)',
                                          mode='markers', marker={'color': 'orange'}),
                             3, 1)

        if type == 'tick':
            self._add_figure(name="wheel_ticks", figure=figure, title="Measurements: Wheel Encoder Ticks")
        else:
            self._add_figure(name="wheel_speed", figure=figure, title="Measurements: Wheel Speed")

    def plot_imu(self):
        """!
        @brief Plot the IMU data.
        """
        if self.output_dir is None:
            return

        # Read the data.
        result = self.reader.read(message_types=[IMUMeasurement], **self.params)
        data = result[IMUMeasurement.MESSAGE_TYPE]

        if len(data.p1_time) == 0:
            self.logger.info('No IMU data available. Skipping plot.')
            return

        time = data.p1_time - float(self.t0)

        figure = make_subplots(rows=2, cols=1, print_grid=False, shared_xaxes=True,
                               subplot_titles=['Acceleration', 'Gyro'])

        figure['layout'].update(showlegend=True)
        figure['layout']['xaxis1'].update(title="Time (sec)", showticklabels=True)
        figure['layout']['xaxis2'].update(title="Time (sec)", showticklabels=True)
        figure['layout']['yaxis1'].update(title="Acceleration (m/s^2)")
        figure['layout']['yaxis2'].update(title="Rotation Rate (rad/s)")

        figure.add_trace(go.Scattergl(x=time, y=data.accel_mps2[0, :], name='X', legendgroup='x',
                                      mode='lines', line={'color': 'red'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=data.accel_mps2[1, :], name='Y', legendgroup='y',
                                      mode='lines', line={'color': 'green'}),
                         1, 1)
        figure.add_trace(go.Scattergl(x=time, y=data.accel_mps2[2, :], name='Z', legendgroup='z',
                                      mode='lines', line={'color': 'blue'}),
                         1, 1)

        figure.add_trace(go.Scattergl(x=time, y=data.gyro_rps[0, :], name='X', legendgroup='x',
                                      showlegend=False, mode='lines', line={'color': 'red'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=data.gyro_rps[1, :], name='Y', legendgroup='y',
                                      showlegend=False, mode='lines', line={'color': 'green'}),
                         2, 1)
        figure.add_trace(go.Scattergl(x=time, y=data.gyro_rps[2, :], name='Z', legendgroup='z',
                                      showlegend=False, mode='lines', line={'color': 'blue'}),
                         2, 1)

        self._add_figure(name="imu", figure=figure, title="Measurements: IMU")

    def generate_event_table(self):
        """!
        @brief Generate a table of event notifications.
        """
        if self.output_dir is None:
            return

        # Read the data.
        result = self.reader.read(message_types=[EventNotificationMessage], remove_nan_times=False, **self.params)
        data = result[EventNotificationMessage.MESSAGE_TYPE]

        if len(data.messages) == 0:
            self.logger.info('No event notification data available.')
            return

        table_columns = ['Relative Time (s)', 'System Time (s)', 'Event', 'Flags', 'Description']
        table_data = [[], [], [], [], []]
        table_data[0] = [f'{(m.system_time_ns - self.reader.get_system_t0_ns()) / 1e9:.3f}' for m in data.messages]
        table_data[1] = [f'{(m.system_time_ns) / 1e9:.3f}' for m in data.messages]
        table_data[2] = [str(m.action) for m in data.messages]
        table_data[3] = [f'0x{m.event_flags:016X}' for m in data.messages]
        table_data[4] = [m.event_description.decode('utf-8') for m in data.messages]

        table_html = _data_to_table(table_columns, table_data)
        body_html = f"""\
<h2>Device Event Log</h2>
<pre>{table_html}</pre>
"""

        self._add_page(name='event_log', html_body=body_html, title="Event Log")

    def generate_index(self, auto_open=True):
        """!
        @brief Generate an `index.html` page with links to all generated figures.

        @param auto_open If `True`, open the page automatically in a web browser.
        """
        if len(self.plots) == 0:
            self.logger.warning('No plots generated. Index will contain summary only.')

        self._set_data_summary()

        if self._mapbox_token_missing:
            self.summary += """\n
<p style="color: red">
  Warning: Mapbox token not specified. Generated map using Open Street Maps
  street data. For satellite imagery, please request a free access token from
  https://account.mapbox.com/access-tokens, then provide the token by
  specifying --mapbox-token or setting the MAPBOX_ACCESS_TOKEN environment
  variable.
</p>
"""

        index_path = os.path.join(self.output_dir, self.prefix + 'index.html')
        index_dir = os.path.dirname(index_path)

        links = ''
        title_to_name = {e['title']: n for n, e in self.plots.items()}
        titles = sorted(title_to_name.keys())
        for title in titles:
            name = title_to_name[title]
            entry = self.plots[name]
            link = '<br><a href="%s" target="_blank">%s</a>' % (os.path.relpath(entry['path'], index_dir), title)
            links += link

        index_html = _page_template % {
            'title': 'FusionEngine Output',
            'body': links + '\n<pre>' + self.summary.replace('\n', '<br>') + '</pre>'
        }

        os.makedirs(index_dir, exist_ok=True)
        with open(index_path, 'w') as f:
            self.logger.info('Creating %s...' % index_path)
            f.write(index_html)

        if auto_open:
            self._open_browser(index_path)

    def _set_data_summary(self):
        # Generate an index file, which we need to calculate the log duration, in case it wasn't created earlier (i.e.,
        # we didn't read anything to plot).
        self.reader.generate_index()

        # Restrict the index to the user-requested time range.
        if self.params['absolute_time']:
            start_time, end_time = self.params['time_range']
        else:
            time_range = self.params['time_range']
            start_time = None if time_range[0] is None else (time_range[0] + float(self.t0))
            end_time = None if time_range[1] is None else (time_range[1] + float(self.t0))

        full_index = self.reader.index
        reduced_index = full_index[start_time:end_time]

        # Calculate the log duration.
        idx = ~np.isnan(full_index['time'])
        time = full_index['time'][idx]
        if len(time) >= 2:
            log_duration_sec = time[-1] - time[0]
        else:
            log_duration_sec = np.nan

        idx = ~np.isnan(reduced_index['time'])
        time = reduced_index['time'][idx]
        if len(time) >= 2:
            processing_duration_sec = time[-1] - time[0]
        else:
            processing_duration_sec = np.nan

        # Create a table with position solution type statistics.
        result = self.reader.read(message_types=[PoseMessage], **self.params)
        pose_data = result[PoseMessage.MESSAGE_TYPE]
        num_pose_messages = len(pose_data.solution_type)
        solution_type_count = {}
        for type, info in _SOLUTION_TYPE_MAP.items():
            solution_type_count[info.name] = np.sum(pose_data.solution_type == type)

        types = list(solution_type_count.keys())
        counts = ['%d' % c for c in solution_type_count.values()]
        if num_pose_messages == 0:
            percents = ['N/A' for c in solution_type_count.values()]
        else:
            percents = ['%.1f%%' % (float(c) / num_pose_messages * 100.0) for c in solution_type_count.values()]

        types.append(None)
        counts.append(None)
        percents.append(None)

        types.append('Total')
        counts.append('%d' % num_pose_messages)
        percents.append('')

        types.append('Processed Duration')
        counts.append('%.1f seconds' % processing_duration_sec)
        percents.append('')

        types.append('Total Log Duration')
        counts.append('%.1f seconds' % log_duration_sec)
        percents.append('')

        solution_type_table = _data_to_table(['Position Type', 'Count', 'Percent'], [types, counts, percents])

        # Create a table with the types and counts of each FusionEngine message type in the log.
        message_types, message_counts = np.unique(reduced_index['type'], return_counts=True)
        message_types = [MessageType.get_type_string(t) for t in message_types]

        message_counts = message_counts.tolist()
        message_types.append(None)
        message_counts.append(None)

        message_types.append('Total')
        message_counts.append(f'{len(self.reader.index)}')

        message_table = _data_to_table(['Message Type', 'Count'], [message_types, message_counts])

        # Create a software version table.
        result = self.reader.read(message_types=[VersionInfoMessage.MESSAGE_TYPE], remove_nan_times=False,
                                  **self.params)
        if len(result[VersionInfoMessage.MESSAGE_TYPE].messages) != 0:
            version = result[VersionInfoMessage.MESSAGE_TYPE].messages[-1]
            version_types = {'fw': 'Firmware', 'engine': 'FusionEngine', 'hw': 'Hardware', 'rx': 'GNSS Receiver'}
            version_values = [str(vars(version)[k + '_version_str']) for k in version_types.keys()]
            version_table = _data_to_table(['Type', 'Version'], [list(version_types.values()), version_values])
        else:
            version_table = 'No version information.'

        # Now populate the summary.
        if self.summary != '':
            self.summary += '\n\n'

        args = {
            'message_table': message_table,
            'version_table': version_table,
            'solution_type_table': solution_type_table,
        }

        self.summary += """
%(version_table)s

%(solution_type_table)s

%(message_table)s
""" % args

    def _add_page(self, name, html_body, title=None):
        if title is None:
            title = name

        if name in self.plots:
            raise ValueError('Plot "%s" already exists.' % name)
        elif name == 'index':
            raise ValueError('Plot name cannot be index.')

        path = os.path.join(self.output_dir, self.prefix + name + '.html')
        self.logger.info('Creating %s...' % path)

        table_html = _page_template % {
            'title': title,
            'body': html_body
        }

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fd:
            fd.write(table_html)

        self.plots[name] = {'title': title, 'path': path}

    def _add_figure(self, name, figure, title=None):
        if title is None:
            title = name

        if name in self.plots:
            raise ValueError('Plot "%s" already exists.' % name)
        elif name == 'index':
            raise ValueError('Plot name cannot be index.')

        path = os.path.join(self.output_dir, self.prefix + name + '.html')
        self.logger.info('Creating %s...' % path)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        plotly.offline.plot(
            figure,
            output_type='file',
            filename=path,
            include_plotlyjs=True,
            auto_open=False,
            show_link=False)

        self.plots[name] = {'title': title, 'path': path}

    def _open_browser(self, filename):
        try:
            webbrowser.open("file:///" + os.path.abspath(filename))
        except BaseException:
            self.logger.error("Unable to open web browser.")

    @classmethod
    def get_mapbox_token(cls, token=None):
        # If the user specified a token, use that.
        if token is not None:
            return token

        # Otherwise, check for environment variables.
        token = os.environ.get('MAPBOX_ACCESS_TOKEN', None)
        if token is not None:
            return token

        token = os.environ.get('MapboxAccessToken', None)
        if token is not None:
            return token

        return None

    def _get_t0_for_time_source(self, time_source: SystemTimeSource) -> float:
        if time_source == SystemTimeSource.P1_TIME:
            return float(self.t0)
        elif time_source == SystemTimeSource.GPS_TIME:
            return 0.0
        elif time_source == SystemTimeSource.SENDER_SYSTEM_TIME:
            return 0.0
        elif time_source == SystemTimeSource.TIMESTAMPED_ON_RECEPTION:
            return float(self.system_t0)

    @classmethod
    def _get_measurement_time(cls, data, time_source: SystemTimeSource) -> np.ndarray:
        if time_source == SystemTimeSource.P1_TIME:
            return data.p1_time
        else:
            return data.measurement_time

    @classmethod
    def _time_source_to_display_name(cls, time_source: SystemTimeSource) -> str:
        if time_source == SystemTimeSource.P1_TIME:
            return 'P1'
        elif time_source == SystemTimeSource.GPS_TIME:
            return 'GPS'
        elif time_source == SystemTimeSource.SENDER_SYSTEM_TIME:
            return 'External'
        elif time_source == SystemTimeSource.TIMESTAMPED_ON_RECEPTION:
            return 'System'


def main():
    parser = ArgumentParser(description="""\
Load and display information stored in a FusionEngine binary file.
""")
    parser.add_argument('--absolute-time', '--abs', action='store_true',
                        help="Interpret the timestamps in --time as absolute P1 times. Otherwise, treat them as "
                             "relative to the first message in the file.")
    parser.add_argument('--ignore-index', action='store_true',
                        help="If set, do not load the .p1i index file corresponding with the .p1log data file. If "
                             "specified and a .p1i file does not exist, do not generate one. Otherwise, a .p1i file "
                             "will be created automatically to improve data read speed in the future.")
    parser.add_argument('--mapbox-token', metavar='TOKEN',
                        help="A Mapbox token to use when generating a map. If unspecified, the token will be read from "
                             "the MAPBOX_ACCESS_TOKEN or MapboxAccessToken environment variables if set. If no token "
                             "is available, a map will not be displayed.")
    parser.add_argument('-m', '--measurements', action='store_true',
                        help="Plot incoming measurement data (slow).")
    parser.add_argument('--no-index', action='store_true',
                        help="Do not automatically open the plots in a web browser.")
    parser.add_argument('-o', '--output', type=str, metavar='DIR',
                        help="The directory where output will be stored. Defaults to the current directory, or to "
                             "'<log_dir>/plot_fusion_engine/' if reading from a log.")
    parser.add_argument('-p', '--prefix', metavar='PREFIX',
                        help="If specified, prepend each filename with PREFIX.")
    parser.add_argument('-t', '--time', type=str, metavar='[START][:END]',
                        help="The desired time range to be analyzed. Both start and end may be omitted to read from "
                             "beginning or to the end of the file. By default, timestamps are treated as relative to "
                             "the first message in the file. See --absolute-time.")
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help="Print verbose/trace debugging messages.")

    parser.add_argument('--log-base-dir', metavar='DIR', default=DEFAULT_LOG_BASE_DIR,
                        help="The base directory containing FusionEngine logs to be searched if a log pattern is "
                             "specified.")
    parser.add_argument('log',
                        help="The log to be read. May be one of:\n"
                             "- The path to a .p1log file or a file containing FusionEngine messages and other "
                             "content\n"
                             "- The path to a FusionEngine log directory\n"
                             "- A pattern matching a FusionEngine log directory under the specified base directory "
                             "(see find_fusion_engine_log() and --log-base-dir)")
    options = parser.parse_args()

    # Configure logging.
    if options.verbose >= 1:
        logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(name)s:%(lineno)d - %(message)s',
                            stream=sys.stdout)
        if options.verbose == 1:
            logging.getLogger('point_one.fusion_engine').setLevel(logging.DEBUG)
        else:
            logging.getLogger('point_one.fusion_engine').setLevel(logging.TRACE)
    else:
        logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)

    # Parse the time range.
    if options.time is not None:
        time_range = options.time.split(':')
        if len(time_range) == 0:
            time_range = [None, None]
        elif len(time_range) == 1:
            time_range.append(None)
        elif len(time_range) == 2:
            pass
        else:
            raise ValueError('Invalid time range specification.')

        for i in range(2):
            if time_range[i] is not None:
                if time_range[i] == '':
                    time_range[i] = None
                else:
                    time_range[i] = float(time_range[i])
                    if time_range[i] < 0.0:
                        time_range[i] = None
    else:
        time_range = None

    # Locate the input file and set the output directory.
    input_path, output_dir, log_id = locate_log(input_path=options.log, log_base_dir=options.log_base_dir,
                                                return_output_dir=True, return_log_id=True)
    if input_path is None:
        # locate_log() will log an error.
        sys.exit(1)

    if log_id is None:
        _logger.info('Loading %s.' % input_path)
    else:
        _logger.info('Loading %s (log ID: %s).' % (input_path, log_id))

    if options.output is None:
        if log_id is not None:
            output_dir = os.path.join(output_dir, 'plot_fusion_engine')
    else:
        output_dir = options.output

    # Read pose data from the file.
    analyzer = Analyzer(file=input_path, output_dir=output_dir, ignore_index=options.ignore_index,
                        prefix=options.prefix + '.' if options.prefix is not None else '',
                        time_range=time_range, absolute_time=options.absolute_time)

    analyzer.plot_time_scale()
    analyzer.plot_solution_type()
    analyzer.plot_pose()
    analyzer.plot_pose_displacement()
    analyzer.plot_relative_position_to_base_station()
    analyzer.plot_map(mapbox_token=options.mapbox_token)
    analyzer.plot_calibration()

    if options.measurements:
        analyzer.plot_imu()
        analyzer.plot_wheel_data()

    analyzer.generate_event_table()

    analyzer.generate_index(auto_open=not options.no_index)

    _logger.info("Output stored in '%s'." % os.path.abspath(output_dir))


if __name__ == "__main__":
    main()
