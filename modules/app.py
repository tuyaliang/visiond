import errno
import gi
import glob
import io
import os
import re
import time
import v4l2
import logging
from fcntl import ioctl

from .config import *
from .streamer import *

### Main visiond App Class
class visiondApp():
    def __init__(self, config, logger):
        self.stdin_path = '/dev/null'
        self.stdout_path = '/srv/maverick/var/log/vision/maverick-visiond.daemon.log'
        self.stderr_path = '/srv/maverick/var/log/vision/maverick-visiond.daemon.log'
        self.pidfile_path = '/srv/maverick/var/run/maverick-visiond.pid'
        self.pidfile_timeout = 5
        self.config = config
        self.logger = logger
        self.setup_gst()

    def setup_gst(self):
        gi.require_version('Gst', '1.0')
        gi.require_version('GstRtspServer', '1.0')
        from gi.repository import GObject,GLib,Gst,GstRtspServer
        Gst.init(None)
        
    def run(self):
        self.logger.handle.info("Starting maverick-visiond")

        if 'debug' in self.config.args and self.config.args.debug:
            Gst.debug_set_active(True)
            Gst.debug_set_default_threshold(self.config.args.debug)
        
        if 'retry' not in self.config.args or not self.config.args.retry:
            self.retry = 30
        else:
            self.retry = self.config.args.retry

        # Start the pipeline.  Trap any errors and wait for 30sec before trying again.
        while True:
            try:
                if 'pipeline_override' in self.config.args:
                    self.logger.handle.info("pipeline_override set, constructing manual pipeline")
                    self.manualconstruct()
                else:
                    self.logger.handle.info("pipeline_override is not set, auto-constructing pipeline")
                    self.autoconstruct()
            except ValueError as e:
                self.logger.handle.critical("Error constructing pipeline: {}, retrying in {} sec".format(repr(e), self.retry))
                time.sleep(float(self.retry))

    def manualconstruct(self):
        if self.config.args.pipeline_override not in self.config.args:
            self.logger.handle.critical('manualconstruct() called but no pipeline_override config argument specified')
            sys.exit(1)
        self.logger.handle.info("Manual Pipeline Construction")
        self.logger.handle.info("Creating pipeline from config: " + self.config.args.pipeline_override)
        try:
            # Create the pipeline from config override
            self.pipeline = Gst.parse_launch(self.config.args.pipeline_override)
            # Set pipeline to playing
            self.pipeline.set_state(Gst.State.PLAYING)
        except Exception as e:
            raise ValueError('Error constructing manual pipeline specified: {}'.format(repr(e)))
        while True:
            time.sleep(5)

    def autoconstruct(self):
        # If camera device set in config use it, otherwise autodetect
        cameradev = None
        devicepaths = glob.glob("/dev/video*")
        try:
            if self.config.args.camera_device:
                cameradev = self.config.args.camera_device
        except:
            # device not set, carry on and try to autodetect
            for devicepath in sorted(devicepaths):
                if not cameradev and self.check_input(devicepath):
                    cameradev = devicepath
                    self.logger.handle.info('v4l2 device '+devicepath+' is a camera, autoselecting')
                elif not cameradev:
                    self.logger.handle.debug('v4l2 device '+devicepath+' is not a camera, ignoring')
        if not cameradev:
            raise ValueError('Error detecting camera video device')

        # Check the camera has a valid input
        try:
            #self.vd = open(cameradev, 'r+')
            self.vd = io.TextIOWrapper(open(cameradev, "r+b", buffering=0))
            cp = v4l2.v4l2_capability()
        except Exception as e:
            raise ValueError("Camera not specified in config, or camera not valid: {}".format(repr(e)))
        if not self.check_input():
            raise ValueError('Specified camera not valid')

        # Log info
        self.camera_info()
        
        # Try and autodetect MFC device
        self.mfcdev = None
        for devicepath in devicepaths:
            dp = io.TextIOWrapper(open(devicepath, "r+b", buffering=0))
            ioctl(dp, v4l2.VIDIOC_QUERYCAP, cp)
            if cp.card == "s5p-mfc-enc":
                self.mfcdev = dp
                self.logger.handle.info('MFC Hardware encoder detected, autoselecting '+devicepath)

        # If format set in config use it, otherwise autodetect
        streamtype = None
        try:
            if self.config.args.format:
                streamtype = self.config.args.format
        except:
            if re.search("C920", self.card.decode()):
                self.logger.handle.info("Logitech C920 detected, forcing H264 passthrough")
                streamtype = 'h264'                                                                     
            # format not set, carry on and try to autodetect
            elif self.check_format('yuv'):
                self.logger.handle.info('Camera YUV stream available, using yuv stream')
                streamtype = 'yuv'
            # Otherwise, check for an mjpeg->h264 encoder pipeline.
            elif self.check_format('mjpeg'):
                self.logger.handle.info('Camera MJPEG stream available, using mjpeg stream')
                streamtype = 'mjpeg'
            # Lastly look for a h264 stream
            elif self.check_format('h264'):
                self.logger.handle.info('Camera H264 stream available, using H264 stream')
                streamtype = 'h264'
        if not streamtype:
            raise ValueError('Error detecting camera video format')

        # If encoder set in config use it, otherwise set to h264
        encoder = None
        try:
            if self.config.args.encoder:
                encoder = self.config.args.encoder
        except:
            pass
        if not encoder:
            encoder = "h264"
        self.logger.handle.debug("Using encoder: {}".format(encoder))
        
        # If raspberry camera detected set pixelformat to I420, otherwise set to YUY2 by default
        pixelformat = "YUY2"
        ioctl(self.vd, v4l2.VIDIOC_QUERYCAP, cp) 
        if cp.driver == "bm2835 mmal":
            self.logger.handle.info("Raspberry Pi Camera detected, setting pixel format to I420")
            pixelformat = "I420"
            
        # If raw pixelformat set in config override the defaults
        if 'pixelformat' in self.config.args:
                pixelformat = self.config.args.pixelformat
        self.logger.handle.debug("Using pixelformat: {}".format(pixelformat))

        # Create the stream
        try:
            self.logger.handle.info("Creating stream object - camera:"+cameradev+", stream:"+streamtype+", pixelformat:"+pixelformat+", encoder:"+encoder+", size:("+str(self.config.args.width)+" x "+str(self.config.args.height)+" / "+str(self.config.args.framerate)+"), output:"+self.config.args.output+", brightness:"+str(self.config.args.brightness))
            Streamer(self.config, self.logger, self.config.args.width, self.config.args.height, self.config.args.framerate, streamtype, pixelformat, encoder, self.config.args.input, cameradev, int(self.config.args.brightness), self.config.args.output, self.config.args.output_dest, int(self.config.args.output_port))
        except Exception as e:
            #self.logger.handle.critical('Error creating '+streamtype+' stream:', traceback.print_exc())
            raise ValueError('Error creating '+streamtype+' stream: ' + str(repr(e)))

        while True:
            time.sleep(5)

    def camera_info(self):
        # Log capability info
        cp = v4l2.v4l2_capability() 
        ioctl(self.vd, v4l2.VIDIOC_QUERYCAP, cp) 
        self.logger.handle.debug("driver: " + cp.driver.decode())
        self.logger.handle.debug("card: " + cp.card.decode())
        self.driver = cp.driver
        self.card = cp.card
        
        # Log controls available
        queryctrl = v4l2.v4l2_queryctrl(v4l2.V4L2_CID_BASE)
        while queryctrl.id < v4l2.V4L2_CID_LASTP1:
            try:
                ioctl(self.vd, v4l2.VIDIOC_QUERYCTRL, queryctrl)
            except IOError as e:
                # this predefined control is not supported by this device
                assert e.errno == errno.EINVAL
                queryctrl.id += 1
                continue
            self.logger.handle.debug("Camera control: " + queryctrl.name.decode())
            queryctrl = v4l2.v4l2_queryctrl(queryctrl.id + 1)
        queryctrl.id = v4l2.V4L2_CID_PRIVATE_BASE
        while True:
            try:
                ioctl(self.vd, v4l2.VIDIOC_QUERYCTRL, queryctrl)
            except IOError as e:
                # no more custom controls available on this device
                assert e.errno == errno.EINVAL
                break
            self.logger.handle.debug("Camera control: " + queryctrl.name.decode())
            queryctrl = v4l2.v4l2_queryctrl(queryctrl.id + 1)
        
        # Log formats available
        capture = v4l2.v4l2_fmtdesc()
        capture.index = 0
        capture.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        try:
            while (ioctl(self.vd, v4l2.VIDIOC_ENUM_FMT, capture) >= 0):
                    self.logger.handle.debug("Camera format: " + capture.description.decode())
                    capture.index += 1
        except:
            pass
        
    def check_input(self, vd=None, index=0):
        if vd == None:
            vd = self.vd
        else:
            vd = io.TextIOWrapper(open(vd, "r+b", buffering=0))
        input = v4l2.v4l2_input(index)
        try:
            ioctl(vd, v4l2.VIDIOC_ENUMINPUT, input)
            self.logger.handle.debug('V4l2 device input: ' + input.name.decode() + ':' + str(input.type))
            if input.type != 2:
                return False # If input type is not camera (2) then return false
            return True
        except Exception as e:
            self.logger.handle.debug("Error checking input: {}".format(repr(e)))
            return False

    def check_format(self, format):
        capture = v4l2.v4l2_fmtdesc()
        capture.index = 0
        capture.type = v4l2.V4L2_BUF_TYPE_VIDEO_CAPTURE
        available = False
        try:
            while (ioctl(self.vd, v4l2.VIDIOC_ENUM_FMT, capture) >= 0):
                self.logger.handle.debug("Format: {} : {}".format(format, capture.description.decode()))
                if format.lower() == "h264":
                    if re.search('H264', capture.description.decode().lower()) or re.search('H.264', capture.description.decode().lower()):
                        available = True
                elif format.lower() == "mjpeg":
                    if re.search('jpeg', capture.description.decode().lower()):
                        available = True
                elif format.lower() == "yuv" or format.lower() == "raw":
                    if re.search('^yu', capture.description.decode().lower()):
                        available = True
                else:
                    if re.search(format.lower(), capture.description.decode().lower()):
                        available = True
                capture.index += 1
        except:
            pass
        return available