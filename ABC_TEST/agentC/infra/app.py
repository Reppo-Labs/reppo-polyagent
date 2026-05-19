#!/usr/bin/env python3
"""CDK app entry for Agent C (ABC experiment)."""
import aws_cdk as cdk

from stack import AgentCStack

app = cdk.App()
AgentCStack(app, "ABC-AgentC", env=cdk.Environment(region="eu-west-1"))
app.synth()
