#!/opt/homebrew/bin/python3.13
import aws_cdk as cdk

from stack import GeoTradingStack

app = cdk.App()
GeoTradingStack(app, "GeoTrading", env=cdk.Environment(region="eu-west-1"))
app.synth()
