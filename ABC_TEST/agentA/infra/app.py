#!/usr/bin/env python3
"""CDK app entry for Agent A (ABC experiment)."""
import aws_cdk as cdk

from stack import AgentAStack

app = cdk.App()
AgentAStack(app, "ABC-AgentA", env=cdk.Environment(region="eu-west-1"))
app.synth()
