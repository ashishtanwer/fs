#!/usr/bin/env python

import logging

log_format='%(asctime)s %(name)-5s %(levelname)-8s %(message)s'

def get_logger(debug=False):
    logger = logging.getLogger('fs')
    loglevel = logging.INFO
    if debug:
        loglevel = logging.DEBUG
    logging.basicConfig(level=loglevel, format=log_format)
    return logger


__obj = None
def set_fscore(obj):
    global __obj
    __obj = obj

def fscore():
    return __obj