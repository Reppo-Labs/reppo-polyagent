#!/usr/bin/env python3
"""CDK app entry for Agent B (ABC experiment)."""
import aws_cdk as cdk

from stack import AgentBStack

app = cdk.App()
AgentBStack(app, "ABC-AgentB", env=cdk.Environment(region="eu-west-1"))
app.synth()
