#!/usr/bin/env python3
"""Raw-capture coordinator using its independently reviewed policy and state."""
from tools.datamax_connector.ai_bot_review.coordinator import Coordinator as BaseCoordinator
class Coordinator(BaseCoordinator):
    def __init__(self,database,api,datamax,clock=None):
        kwargs={"stream":"raw_capture"}
        if clock is not None:kwargs["clock"]=clock
        super().__init__(database,api,datamax,**kwargs)
