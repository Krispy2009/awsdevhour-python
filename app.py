#!/usr/bin/env python3

from aws_cdk import core

from awsdevhour.awsdevhour_stack import AwsdevhourStack


app = core.App()
AwsdevhourStack(app, "awsdevhour")

app.synth()
