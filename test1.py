# Pipeline for Video Input
# Write configuration texts, UID is set to PGIE, update parse_args()

import sys
from pathlib import Path
import gi
import configparser
import argparse
from gi.repository import GLib, Gst
from ctypes import *
import time
import math
import platform
from common.is_aarch_64 import is_aarch64
from common.bus_call import bus_call
from common.FPS import PERF_DATA

import pyds

sys.path.append('../')
gi.require_version('Gst', '1.0')

no_display = False
silent = False
file_loop = False
perf_data = None

# PGIE implements PeopleNet

PRIMARY_DETECTOR_UID = 1
MAX_DISPLAY_LEN = 64
PGIE_CLASS_ID_PERSON = 0
PGIE_CLASS_ID_BAG = 1
PGIE_CLASS_ID_FACE = 2
MUXER_OUTPUT_WIDTH = 1920
MUXER_OUTPUT_HEIGHT = 1080
MUXER_BATCH_TIMEOUT_USEC = 4000000
GST_CAPS_FEATURES_NVMM = "memory:NVMM"
OSD_PROCESS_MODE = 0
OSD_DISPLAY_TEXT = 1
pgie_classes_str = ["person", "bag", "face"]


# pgie_src_pad_buffer_probe  will extract metadata received on tiler sink pad
# and update params for drawing rectangle, object information etc.
def sgie_4_src_pad_buffer_probe(pad, info, u_data):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        frame_number = frame_meta.frame_num
        l_obj = frame_meta.obj_meta_list
        num_rects = frame_meta.num_obj_meta
        obj_counter = {
            PGIE_CLASS_ID_PERSON: 0,
            PGIE_CLASS_ID_BAG: 0,
            PGIE_CLASS_ID_FACE: 0
        }
        while l_obj is not None:
            try:
                # Casting l_obj.data to pyds.NvDsObjectMeta
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
            except StopIteration:
                break

            if obj_meta.unique_component_id == PRIMARY_DETECTOR_UID:
                obj_counter[obj_meta.class_id] += 1

            try:
                l_obj = l_obj.next
            except StopIteration:
                break
        if not silent:
            print("Frame Number=", frame_number, "Number of Objects=", num_rects, "Person_count=",
                  obj_counter[PGIE_CLASS_ID_PERSON])

        # Update frame rate through this probe
        stream_index = "stream{0}".format(frame_meta.pad_index)
        global perf_data
        perf_data.update_fps(stream_index)

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK


def cb_newpad(decodebin, decoder_src_pad, data):
    print("In cb_newpad\n")
    caps = decoder_src_pad.get_current_caps()
    if not caps:
        caps = decoder_src_pad.query_caps()
    gststruct = caps.get_structure(0)
    gstname = gststruct.get_name()
    source_bin = data
    features = caps.get_features(0)

    # Need to check if the pad created by the decodebin is for video and not
    # audio.
    print("gstname=", gstname)
    if (gstname.find("video") != -1):
        # Link the decodebin pad only if decodebin has picked nvidia
        # decoder plugin nvdec_*. We do this by checking if the pad caps contain
        # NVMM memory features.
        print("features=", features)
        if features.contains("memory:NVMM"):
            # Get the source bin ghost pad
            bin_ghost_pad = source_bin.get_static_pad("src")
            if not bin_ghost_pad.set_target(decoder_src_pad):
                sys.stderr.write("Failed to link decoder src pad to source bin ghost pad\n")
        else:
            sys.stderr.write(" Error: Decodebin did not pick nvidia decoder plugin.\n")


def decodebin_child_added(child_proxy, Object, name, user_data):
    print("Decodebin child added:", name, "\n")
    if (name.find("decodebin") != -1):
        Object.connect("child-added", decodebin_child_added, user_data)

    if "source" in name:
        source_element = child_proxy.get_by_name("source")
        if source_element.find_property('drop-on-latency') != None:
            Object.set_property("drop-on-latency", True)


def create_source_bin(index, uri):
    print("Creating source bin")

    # Create a source GstBin to abstract this bin's content from the rest of the
    # pipeline
    bin_name = "source-bin-%02d" % index
    print(bin_name)
    nbin = Gst.Bin.new(bin_name)
    if not nbin:
        sys.stderr.write(" Unable to create source bin \n")

    if file_loop:
        # use nvurisrcbin to enable file-loop
        uri_decode_bin = Gst.ElementFactory.make("nvurisrcbin", "uri-decode-bin")
        uri_decode_bin.set_property("file-loop", 1)
    else:
        uri_decode_bin = Gst.ElementFactory.make("uridecodebin", "uri-decode-bin")
    if not uri_decode_bin:
        sys.stderr.write(" Unable to create uri decode bin \n")
    # We set the input uri to the source element
    uri_decode_bin.set_property("uri", uri)
    # Connect to the "pad-added" signal of the decodebin which generates a
    # callback once a new pad for raw data has beed created by the decodebin
    uri_decode_bin.connect("pad-added", cb_newpad, nbin)
    uri_decode_bin.connect("child-added", decodebin_child_added, nbin)
    Gst.Bin.add(nbin, uri_decode_bin)
    bin_pad = nbin.add_pad(Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC))
    if not bin_pad:
        sys.stderr.write(" Failed to add ghost pad in source bin \n")
        return None
    return nbin


def main(args):
    global perf_data
    perf_data = PERF_DATA(len(args))

    number_sources = len(args)

    # Standard GStreamer initialization
    Gst.init(None)

    # Create gstreamer elements */
    # Create Pipeline element that will form a connection of other elements
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    is_live = False

    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
    print("Creating streamux \n ")

    # Create nvstreammux instance to form batches from one or more sources.
    streammux = Gst.ElementFactory.make("nvstreammux", "Stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create NvStreamMux \n")

    pipeline.add(streammux)
    for i in range(number_sources):
        print("Creating source_bin ", i, " \n ")
        uri_name = args[i]
        if uri_name.find("rtsp://") == 0:
            is_live = True
        source_bin = create_source_bin(i, uri_name)
        if not source_bin:
            sys.stderr.write("Unable to create source bin \n")
        pipeline.add(source_bin)
        padname = "sink_%u" % i
        sinkpad = streammux.get_request_pad(padname)
        if not sinkpad:
            sys.stderr.write("Unable to create sink pad bin \n")
        srcpad = source_bin.get_static_pad("src")
        if not srcpad:
            sys.stderr.write("Unable to create src pad bin \n")
        srcpad.link(sinkpad)
    queue1 = Gst.ElementFactory.make("queue", "queue1")
    queue2 = Gst.ElementFactory.make("queue", "queue2")
    queue3 = Gst.ElementFactory.make("queue", "queue3")
    queue4 = Gst.ElementFactory.make("queue", "queue4")
    queue5 = Gst.ElementFactory.make("queue", "queue5")
    queue6 = Gst.ElementFactory.make("queue", "queue6")
    pipeline.add(queue1)
    pipeline.add(queue2)
    pipeline.add(queue3)
    pipeline.add(queue4)
    pipeline.add(queue5)
    pipeline.add(queue6)

    print("Creating Pgie \n ")
    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")

    if not pgie:
        sys.stderr.write(" Unable to create pgie")

    print("Creating Sgie1 \n ")
    sgie_1 = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not sgie_1:
        sys.stderr.write(" Unable to create Sgie1")

    print("Creating Sgie2 \n ")
    sgie_2 = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not sgie_2:
        sys.stderr.write(" Unable to create Sgie2")

    print("Creating Sgie3 \n ")
    sgie_3 = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not sgie_3:
        sys.stderr.write(" Unable to create Sgie3")

    print("Creating Sgie4 \n ")
    sgie_4 = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not sgie_4:
        sys.stderr.write(" Unable to create Sgie4")

    print("Creating Fakesink \n")
    sink = Gst.ElementFactory.make("fakesink", "fakesink")
    sink.set_property('enable-last-sample', 0)
    sink.set_property('sync', 0)

    if not sink:
        sys.stderr.write(" Unable to create sink element \n")

    if is_live:
        print("At least one of the sources is live")
        streammux.set_property('live-source', 1)

    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batch-size', number_sources)
    streammux.set_property('batched-push-timeout', 4000000)
    pgie.set_property('config-file-path', "video_pgie_config.txt")
    pgie_batch_size = pgie.get_property("batch-size")
    if pgie_batch_size != number_sources:
        print("WARNING: Overriding infer-config batch-size", pgie_batch_size, " with number of sources ",
              number_sources, " \n")
        pgie.set_property("batch-size", number_sources)

    sgie_1.set_property('config-file-path', "video_sgie_1_config.txt")
    sgie_1_batch_size = sgie_1.get_property("batch-size")
    if sgie_1_batch_size != number_sources:
        print("WARNING: Overriding infer-config batch-size", sgie_1_batch_size, " with number of sources ",
              number_sources, " \n")
        sgie_1.set_property("batch-size", number_sources)

    sgie_2.set_property('config-file-path', "video_sgie_2_config.txt")
    sgie_2_batch_size = sgie_2.get_property("batch-size")
    if sgie_2_batch_size != number_sources:
        print("WARNING: Overriding infer-config batch-size", sgie_2_batch_size, " with number of sources ",
              number_sources, " \n")
        sgie_2.set_property("batch-size", number_sources)

    sgie_3.set_property('config-file-path', "video_sgie_3_config.txt")
    sgie_3_batch_size = sgie_3.get_property("batch-size")
    if sgie_3_batch_size != number_sources:
        print("WARNING: Overriding infer-config batch-size", sgie_3_batch_size, " with number of sources ",
              number_sources, " \n")
        sgie_3.set_property("batch-size", number_sources)

    sgie_4.set_property('config-file-path', "video_sgie_4_config.txt")
    sgie_4_batch_size = sgie_4.get_property("batch-size")
    if sgie_4_batch_size != number_sources:
        print("WARNING: Overriding infer-config batch-size", sgie_4_batch_size, " with number of sources ",
              number_sources, " \n")
        sgie_4.set_property("batch-size", number_sources)

    sink.set_property("qos", 0)

    print("Adding elements to Pipeline \n")
    pipeline.add(pgie)
    pipeline.add(sgie_1)
    pipeline.add(sgie_2)
    pipeline.add(sgie_3)
    pipeline.add(sgie_4)
    pipeline.add(sink)

    print("Linking elements in the Pipeline \n")
    streammux.link(queue1)
    queue1.link(pgie)
    pgie.link(queue2)
    queue2.link(sgie_1)
    sgie_1.link(queue3)
    queue3.link(sgie_2)
    sgie_2.link(queue4)
    queue4.link(sgie_3)
    sgie_3.link(queue5)
    queue5.link(sgie_4)
    sgie_4.link(queue6)
    queue6.link(sink)

    # create an event loop and feed gstreamer bus mesages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)
    sgie_4_src_pad = sgie_4.get_static_pad("src")
    if not sgie_4_src_pad:
        sys.stderr.write(" Unable to get src pad \n")
    else:
        sgie_4_src_pad.add_probe(Gst.PadProbeType.BUFFER, sgie_4_src_pad_buffer_probe, 0)
        # perf callback function to print fps every 5 sec
        GLib.timeout_add(5000, perf_data.perf_print_callback)

    # List the sources
    print("Now playing...")
    for i, source in enumerate(args):
        print(i, ": ", source)

    print("Starting pipeline \n")
    # start play back and listed to events
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass
    # cleanup
    print("Exiting app\n")
    pipeline.set_state(Gst.State.NULL)


# configuration function block

def parse_args():
    parser = argparse.ArgumentParser(prog="deepstream_test_3",
                                     description="deepstream-test3 multi stream, multi model inference reference app")
    parser.add_argument(
        "-i",
        "--input",
        help="Path to input streams",
        nargs="+",
        metavar="URIs",
        default=["a"],
        required=True,
    )
    parser.add_argument(
        "-c",
        "--configfile",
        metavar="config_location.txt",
        default=None,
        help="Choose the config-file to be used with specified pgie",
    )
    parser.add_argument(
        "-g",
        "--pgie",
        default=None,
        help="Choose Primary GPU Inference Engine",
        choices=["nvinfer", "nvinferserver", "nvinferserver-grpc"],
    )
    parser.add_argument(
        "--no-display",
        action="store_true",
        default=False,
        dest='no_display',
        help="Disable display of video output",
    )
    parser.add_argument(
        "--file-loop",
        action="store_true",
        default=False,
        dest='file_loop',
        help="Loop the input file sources after EOS",
    )
    parser.add_argument(
        "--disable-probe",
        action="store_true",
        default=False,
        dest='disable_probe',
        help="Disable the probe function and use nvdslogger for FPS",
    )
    parser.add_argument(
        "-s",
        "--silent",
        action="store_true",
        default=False,
        dest='silent',
        help="Disable verbose output",
    )
    # Check input arguments
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()

    stream_paths = args.input
    pgie = args.pgie
    config = args.configfile
    disable_probe = args.disable_probe
    global no_display
    global silent
    global file_loop
    no_display = args.no_display
    silent = args.silent
    file_loop = args.file_loop

    if config and not pgie or pgie and not config:
        sys.stderr.write("\nEither pgie or configfile is missing. Please specify both! Exiting...\n\n\n\n")
        parser.print_help()
        sys.exit(1)
    if config:
        config_path = Path(config)
        if not config_path.is_file():
            sys.stderr.write("Specified config-file: %s doesn't exist. Exiting...\n\n" % config)
            sys.exit(1)

    print(vars(args))
    return stream_paths, pgie, config, disable_probe


if __name__ == '__main__':
    stream_paths, pgie, config, disable_probe = parse_args()
    sys.exit(main(stream_paths))

