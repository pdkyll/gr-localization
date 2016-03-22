#!/usr/bin/env python

###############################################################################
# Imports
###############################################################################
from gnuradio import zeromq
from gnuradio import gr
from gnuradio import blocks
from gnuradio import analog
from gnuradio import eng_notation
from gnuradio import uhd
from gnuradio.eng_option import eng_option
from optparse import OptionParser
import numpy
import sys
import os
import threading
import time
import socket
import serial
import calendar
import octoclock
sys.path.append("../python")
import rpc_manager as rpc_manager_local

###############################################################################
# GNU Radio top_block
###############################################################################
class top_block(gr.top_block):
    def __init__(self, options):
        gr.top_block.__init__(self)

        self.options = options

        self.run_loop = False

        # socket addresses
        rpc_port = 6665 + options.id_rx
        rpc_adr = "tcp://*:" + str(rpc_port)
        fusion_center_adr = "tcp://" + options.fusion_center + ":6665"
        probe_port = 5555 + options.id_rx
        probe_adr = "tcp://*:" + str(probe_port)

        # blocks
        self.zmq_probe = zeromq.pub_sink(gr.sizeof_gr_complex, 1, probe_adr)
        self.tag_debug = blocks.tag_debug(gr.sizeof_gr_complex*1, "", ""); self.tag_debug.set_display(True)

        if self.options.serial != "":
            if self.options.mcr != 0:
                self.usrp_source = uhd.usrp_source(
                    "serial == " + self.options.serial
                    + ",master_clock_rate == " + str(self.options.mcr),
                    uhd.stream_args(
                        cpu_format="fc32",
                        channels=range(1),
                     ), False
                )
            else:
                self.usrp_source = uhd.usrp_source(
                    "serial == " + self.options.serial,
                    uhd.stream_args(
                        cpu_format="fc32",
                        channels=range(1),
                     ), False
                )

        else:
            if self.options.mcr != 0:
                self.usrp_source = uhd.usrp_source(
                    "master_clock_rate == " + str(self.options.mcr),
                    uhd.stream_args(
                        cpu_format="fc32",
                        channels=range(1),
                    ), False
                )
            else:
                self.usrp_source = uhd.usrp_source(
                    "",
                    uhd.stream_args(
                        cpu_format="fc32",
                        channels=range(1),
                    ), False
                )

        self.gps = options.gps

        if self.gps != "lc_xo":
            print "Using " + self.gps
            self.usrp_source.set_clock_source("external", 0)
            self.usrp_source.set_time_source("external", 0)
        if self.gps == "lte_lite":
            self.ser = serial.Serial("/dev/ttyUSB0", 38400, timeout = 1)
            self.nmea_lte_lite = ""
            self.nmea_lte_lite_lock = threading.Lock()
            threading.Thread(target = self.poll_lte_lite).start()

        # connects
        #self.connect(self.usrp_source, self.s_to_v, self.zmq_probe)
        self.connect(self.usrp_source, self.zmq_probe)
        self.connect(self.usrp_source, self.tag_debug)

        # ZeroMQ
        self.rpc_manager = rpc_manager_local.rpc_manager()
        self.rpc_manager.set_reply_socket(rpc_adr)
        self.rpc_manager.set_request_socket(fusion_center_adr)
        self.rpc_manager.add_interface("set_run_loop",self.set_run_loop)
        self.rpc_manager.add_interface("start_fg",self.start_fg)
        self.rpc_manager.add_interface("set_gain",self.set_gain)
        self.rpc_manager.add_interface("set_samp_rate",self.set_samp_rate)
        self.rpc_manager.add_interface("set_bw",self.set_bw)
        self.rpc_manager.add_interface("set_antenna",self.set_antenna)
        self.rpc_manager.add_interface("get_gps_position",self.get_gps_position)
        self.rpc_manager.start_watcher()


        # Find out ip address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if not options.fusion_center == "localhost":
            s.connect((options.fusion_center,22))
        else:
            s.connect(("www.rwth-aachen.de",80))
        self.ip_addr = s.getsockname()[0]

    def set_run_loop(self, run_loop):
        self.run_loop = run_loop

    def set_samp_rate(self,samp_rate):
        self.usrp_source.set_samp_rate(samp_rate)
        threading.Thread(target = self.sync_time_nmea).start()
    def set_bw(self,bw):
        self.usrp_source.set_bandwidth(bw,0)
    def set_gain(self,gain):
        self.usrp_source.set_gain(gain, 0)
    def set_antenna(self,antenna):
        self.usrp_source.set_antenna(antenna, 0)

    def register_receiver(self):
        first = True
        while(True):
            # register receiver [hostname, usrp_serial, rx_id]
            self.rpc_manager.request("register_receiver",[self.ip_addr, self.usrp_source.get_usrp_info().vals()[2], self.options.id_rx, self.gps, first])
            first = False
            time.sleep(10)

    def start_fg(self, samples_to_receive, freq, lo_offset, bw, gain, samples_to_receive_calibration, freq_calibration, lo_offset_calibration, bw_calibration, gain_calibration, time_to_recv, auto_calibrate, acquisitions):
        threading.Thread(target = self.start_reception, args = (samples_to_receive, freq, lo_offset, bw, gain, samples_to_receive_calibration, freq_calibration, lo_offset_calibration, bw_calibration, gain_calibration, time_to_recv, auto_calibrate, acquisitions)).start()


    def start_reception(self, samples_to_receive, freq, lo_offset, bw, gain, samples_to_receive_calibration, freq_calibration, lo_offset_calibration, bw_calibration, gain_calibration, time_to_recv, auto_calibrate, acquisitions):

        loop_frequency = 2 # seconds between acquisitions

        if acquisitions == 0:
            infinity = True
        else:
            infinity = False

        if time_to_recv is None:
            time_to_recv = self.usrp_source.get_time_last_pps().get_real_secs() + 1
        else:
            time_to_recv = float(time_to_recv)

        time_now = self.usrp_source.get_time_now().get_real_secs()
        if time_to_recv < time_now:
            print time_to_recv,time_now
            print "Can't start in the past!"
            return

        while True:
            time_now = self.usrp_source.get_time_now().get_real_secs()
            if round(time_to_recv) == round(time_now):
                print "Start Flowgraph"
                try:
                    # get times from USRP
                    time_begin = self.usrp_source.get_time_now().get_real_secs()
                    time_last_pps = self.usrp_source.get_time_last_pps().get_real_secs()
                    time_to_sample = uhd.time_spec(time_to_recv + 3 * loop_frequency/10.0)
                    if freq_calibration is not None:
                        time_to_calibrate = uhd.time_spec(time_to_recv + 7 * loop_frequency/10.0)
                    # synchronize LOs
                    self.usrp_source.set_center_freq(uhd.tune_request(freq, lo_offset), 0)
                    self.usrp_source.set_gain(gain,0)
                    self.usrp_source.set_bandwidth(bw,0)
                    # ask for samples at a specific time
                    stream_cmd = uhd.stream_cmd(uhd.stream_cmd_t.STREAM_MODE_NUM_SAMPS_AND_DONE)
                    # add 100 samples to the burst to get rid of transient
                    stream_cmd.num_samps = samples_to_receive + 100
                    stream_cmd.stream_now = False
                    stream_cmd.time_spec = time_to_sample
                    self.usrp_source.issue_stream_cmd(stream_cmd)
                    time_now = self.usrp_source.get_time_now().get_real_secs()
                    time.sleep(abs(time_to_recv - time_now) + 3 * loop_frequency/10.0)
                    if auto_calibrate:
                        # synchronize LOs
                        self.usrp_source.set_center_freq(uhd.tune_request(freq_calibration, lo_offset_calibration), 0)
                        self.usrp_source.set_gain(gain_calibration,0)
                        self.usrp_source.set_bandwidth(bw_calibration,0)
                        # ask for samples at a specific time
                        stream_cmd = uhd.stream_cmd(uhd.stream_cmd_t.STREAM_MODE_NUM_SAMPS_AND_DONE)
                        # add 100 samples to the burst to get rid of transient
                        stream_cmd.num_samps = samples_to_receive_calibration + 100
                        stream_cmd.stream_now = False
                        stream_cmd.time_spec = time_to_calibrate
                        self.usrp_source.issue_stream_cmd(stream_cmd)
                    print "Time begin:", time_begin
                    print "Time last pps:", time_last_pps
                    print "Time to sample:", time_to_sample.get_real_secs()
                    if freq_calibration is not None:
                        print "Time to calibrate:", time_to_calibrate.get_real_secs()
                    usrp = self.usrp_source
                    print "Parameters:", usrp.get_center_freq(0),usrp.get_gain(0),usrp.get_samp_rate(),usrp.get_bandwidth(0),samples_to_receive,usrp.get_antenna(0)
                    acquisitions -= 1
                    if not self.run_loop or (acquisitions <= 0 and not infinity):
                        break
                    time_to_recv = time_to_recv + loop_frequency
                except RuntimeError:
                    print "Can't start, flowgraph already running!"

    def sync_time_nmea(self):
        print "Begin time sync"
        if self.gps == "octoclock":
            clock = octoclock.multi_usrp_clock()
        # get time of last pps from USRP
        last_pps_time = self.usrp_source.get_time_last_pps().get_real_secs()
        print "Last pps time before sync:", last_pps_time
        synced = False
        while not synced:
            if self.gps == "lte_lite":
                while self.nmea_lte_lite is "":
                    time.sleep(0.1)
                self.nmea_lte_lite_lock.acquire()
                s = self.nmea_lte_lite
                self.nmea_lte_lite_lock.release()
                t = s.split(",")[1].split(".")[0]
                d = s.split(",")[9]
                my_time = time.strptime(t+d,"%H%M%S%d%m%y")
                time_nmea = calendar.timegm(my_time)
                self.usrp_source.set_time_next_pps(uhd.time_spec(time_nmea + 1))
                if self.options.ntp_server and time_nmea > 1400000000:
                    os.system("sudo date +%s -s @"+str(time_nmea))
                    print "System time set to:", str(time_nmea)
                print "Set USRP to NMEA time + 1s:", time_nmea + 1
                synced = True
            else:
                # check for occurence of next pps
                last_pps_time_check = self.usrp_source.get_time_last_pps().get_real_secs()
                print "Check last pps time:", last_pps_time_check
                if last_pps_time_check > last_pps_time:
                    # get pps time from NMEA and set time of next pps
                    if self.gps == "octoclock":
                        time_nmea = clock.get_time_real_secs()
                    else:
                        time_nmea = [int(s) for s in self.usrp_source.get_mboard_sensor("gps_time",0).to_pp_string().split() if s.isdigit()][0]
                    # set internal time registers in USRP
                    self.usrp_source.set_time_next_pps(uhd.time_spec(time_nmea + 1))
                    # set system time if ntp server option activated
                    if self.options.ntp_server and time_nmea > 1400000000:
                        os.system("sudo date +%s -s @"+str(time_nmea))
                        print "System time set to:", str(time_nmea)
                    print "Set USRP to NMEA time + 1s:", time_nmea + 1
                    synced = True
                else:
                    # sleep for 100 msec
                    time.sleep(0.1)
        print "Just after NMEA sync: ", self.usrp_source.get_time_last_pps().get_real_secs()
        time.sleep(1)
        print "After 1s: ", self.usrp_source.get_time_last_pps().get_real_secs()
        print "NMEA time sync complete!"
        self.rpc_manager.request("sync_success",[self.ip_addr, self.options.ntp_server])

    def poll_lte_lite(self):
        while True:
            nmea = self.ser.readline().replace("\x00","").replace("\n","")
            if "GPRMC" in nmea and len(nmea.split(",")) ==13 :
                self.nmea_lte_lite_lock.acquire()
                self.nmea_lte_lite = nmea
                self.nmea_lte_lite_lock.release()

    def get_gps_gprmc(self):
        if self.gps == "octoclock":
            clock = octoclock.multi_usrp_clock()
            nmea = clock.get_sensor("gps_gprmc")[11:-1]
        elif self.gps == "lte_lite":
            while self.nmea_lte_lite is "":
                time.sleep(0.1)
            self.nmea_lte_lite_lock.acquire()
            nmea = self.nmea_lte_lite
            self.nmea_lte_lite_lock.release()
        else:
            nmea = self.usrp_source.get_mboard_sensor("gps_gpgga",0).value

        return nmea

    def get_gps_position(self):
        nmea = self.get_gps_gprmc()
        latitude = nmea.split(",")[2:4]
        # the NMEA sentence may vary for each receiver. This code works for 
        # the next structure in latitude DDMM.XXXXXX
        if latitude[1] == "N":
            latitude = int(latitude[0][0:2])+(float(latitude[0][2:])/60)
        else:
            latitude = -int(latitude[0][0:2])-(float(latitude[0][2:])/60)

        longitude = nmea.split(",")[4:6]
        # the NMEA sentence may vary for each receiver. This code works for 
        # the next structure in longitude DDDMM.XXXXXX
        if longitude[1] == "E":
            longitude = int(longitude[0][0:3])+(float(longitude[0][3:])/60)
        else:
            longitude = -int(longitude[0][0:3])-(float(longitude[0][3:])/60)
        return [longitude, latitude]

###############################################################################
# Options Parser
###############################################################################
def parse_options():
    """ Options parser. """
    parser = OptionParser(option_class=eng_option, usage="%prog: [options]")
    parser.add_option("-s", "--serial", type="string", default="",
                      help="USRP serial number")
    parser.add_option("", "--fusion-center", type="string", default="localhost",
                      help="Fusion center address")
    parser.add_option("-g", "--gps", type="string", default="lc_xo",
                      help="GPS type")
    parser.add_option("-i", "--id-rx", type="int", default="1",
                      help="Receiver ID")
    parser.add_option("--mcr", type="float", default="0",
                      help="Master clock rate")
    parser.add_option("", "--dot-graph", action="store_true", default=False,
                      help="Generate dot-graph file from flowgraph")
    parser.add_option("", "--ntp-server", action="store_true", default=False,
                      help="Activate ntp server")
    (options, args) = parser.parse_args()
    return options

###############################################################################
# Main
###############################################################################
if __name__ == "__main__":
    options = parse_options()
    tb = top_block(options)

    if options.dot_graph:
        # write a dot graph of the flowgraph to file
        dot_str = tb.dot_graph()
        file_str = os.path.expanduser('flowgraph.dot')
        dot_file = open(file_str,'w')
        dot_file.write(dot_str)
        dot_file.close()

    try:
        tb.start()
        tb.usrp_source.stop()

        tb.timer_register = threading.Thread(target = tb.register_receiver)
        tb.timer_register.daemon = True
        tb.timer_register.start()
        # keep the program running when flowgraph is stopped
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    print "Shutting down flowgraph."
    tb.rpc_manager.stop_watcher()
    tb.stop()
    tb.wait()
    tb.ser.close()
    tb = None