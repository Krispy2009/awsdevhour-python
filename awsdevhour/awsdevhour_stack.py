from aws_cdk import core as cdk
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_lambda as lb
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda_event_sources as event_sources

IMG_BUCKET_NAME = "cdk-rekn-imagebucket"
RESIZED_IMG_BUCKET_NAME = f"{IMG_BUCKET_NAME}-resized"


class AwsdevhourStack(cdk.Stack):
    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Image Bucket
        image_bucket = s3.Bucket(self, IMG_BUCKET_NAME, removal_policy=cdk.RemovalPolicy.DESTROY)
        cdk.CfnOutput(self, "imageBucket", value=image_bucket.bucket_name)

        # Thumbnail Bucket
        resized_image_bucket = s3.Bucket(
            self, RESIZED_IMG_BUCKET_NAME, removal_policy=cdk.RemovalPolicy.DESTROY
        )
        cdk.CfnOutput(self, "resizedBucket", value=resized_image_bucket.bucket_name)

        # DynamoDB to store image labels
        partition_key = dynamodb.Attribute(name="image", type=dynamodb.AttributeType.STRING)
        table = dynamodb.Table(
            self,
            "ImageLabels",
            partition_key=partition_key,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        cdk.CfnOutput(self, "ddbTable", value=table.table_name)

        # Lambda layer for Pillow library
        layer = lb.LayerVersion(
            self,
            "pil",
            code=lb.Code.from_asset("reklayer"),
            compatible_runtimes=[lb.Runtime.PYTHON_3_7],
            license="Apache-2.0",
            description="A layer to enable the PIL library in our Rekognition Lambda",
        )

        # Lambda function
        rek_fn = lb.Function(
            self,
            "rekognitionFunction",
            code=lb.Code.from_asset("rekognitionFunction"),
            runtime=lb.Runtime.PYTHON_3_7,
            handler="index.handler",
            timeout=cdk.Duration.seconds(30),
            memory_size=1024,
            layers=[layer],
            environment={
                "TABLE": table.table_name,
                "BUCKET": image_bucket.bucket_name,
                "THUMBBUCKET": resized_image_bucket.bucket_name,
            },
        )
        rek_fn.add_event_source(
            event_sources.S3EventSource(image_bucket, events=[s3.EventType.OBJECT_CREATED])
        )
        image_bucket.grant_read(rek_fn)
        table.grant_write_data(rek_fn)

        rek_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW, actions=["rekognition:DetectLabels"], resources=["*"]
            )
        )

        # The code that defines your stack goes here
