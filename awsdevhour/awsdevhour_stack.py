import json
from aws_cdk import core as cdk
import aws_cdk.aws_s3 as s3
import aws_cdk.aws_s3_deployment as s3_dep
import aws_cdk.aws_lambda as lb
import aws_cdk.aws_dynamodb as dynamodb
import aws_cdk.aws_iam as iam
import aws_cdk.aws_lambda_event_sources as event_sources
import aws_cdk.aws_apigateway as apigw
import aws_cdk.aws_cognito as cognito
import aws_cdk.aws_sqs as sqs
import aws_cdk.aws_s3_notifications as s3n

IMG_BUCKET_NAME = "cdk-rekn-imagebucket"
RESIZED_IMG_BUCKET_NAME = f"{IMG_BUCKET_NAME}-resized"
WEBSITE_BUCKET_NAME = "cdk-rekn-publicbucket"


class AwsdevhourStack(cdk.Stack):
    def __init__(self, scope: cdk.Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Image Bucket
        image_bucket = s3.Bucket(self, IMG_BUCKET_NAME, removal_policy=cdk.RemovalPolicy.DESTROY)
        cdk.CfnOutput(self, "imageBucket", value=image_bucket.bucket_name)

        image_bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.PUT],
            allowed_origins=["*"],
            allowed_headers=["*"],
            max_age=3000,
        )

        # Thumbnail Bucket
        resized_image_bucket = s3.Bucket(
            self, RESIZED_IMG_BUCKET_NAME, removal_policy=cdk.RemovalPolicy.DESTROY
        )
        cdk.CfnOutput(self, "resizedBucket", value=resized_image_bucket.bucket_name)

        resized_image_bucket.add_cors_rule(
            allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.PUT],
            allowed_origins=["*"],
            allowed_headers=["*"],
            max_age=3000,
        )
        # S3 Static bucket for website code
        web_bucket = s3.Bucket(
            self,
            WEBSITE_BUCKET_NAME,
            website_index_document="index.html",
            website_error_document="index.html",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            # uncomment this and delete the policy statement below to allow public access to our
            # static website
            # public_read_access=true
        )

        web_policy_statement = iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[web_bucket.arn_for_objects("*")],
            principals=[iam.AnyPrincipal()],
            conditions={"IpAddress": {"aws:SourceIp": ["139.138.203.36"]}},
        )

        web_bucket.add_to_resource_policy(web_policy_statement)

        cdk.CfnOutput(self, "bucketURL", value=web_bucket.bucket_website_domain_name)

        # Deploy site contents to S3 Bucket
        s3_dep.BucketDeployment(
            self,
            "DeployWebsite",
            sources=[s3_dep.Source.asset("./public")],
            destination_bucket=web_bucket,
        )

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

        image_bucket.grant_read(rek_fn)
        resized_image_bucket.grant_write(rek_fn)
        table.grant_write_data(rek_fn)

        rek_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW, actions=["rekognition:DetectLabels"], resources=["*"]
            )
        )

        # Lambda for Synchronous front end
        serviceFn = lb.Function(
            self,
            "serviceFunction",
            code=lb.Code.from_asset("servicelambda"),
            runtime=lb.Runtime.PYTHON_3_7,
            handler="index.handler",
            environment={
                "TABLE": table.table_name,
                "BUCKET": image_bucket.bucket_name,
                "RESIZEDBUCKET": resized_image_bucket.bucket_name,
            },
        )

        image_bucket.grant_write(serviceFn)
        resized_image_bucket.grant_write(serviceFn)
        table.grant_read_write_data(serviceFn)

        # Cognito User Pool Auth
        auto_verified_attrs = cognito.AutoVerifiedAttrs(email=True)
        sign_in_aliases = cognito.SignInAliases(email=True, username=True)
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=True,
            auto_verify=auto_verified_attrs,
            sign_in_aliases=sign_in_aliases,
        )

        user_pool_client = cognito.UserPoolClient(
            self, "UserPoolClient", user_pool=user_pool, generate_secret=False
        )

        identity_pool = cognito.CfnIdentityPool(
            self,
            "ImageRekognitionIdentityPool",
            allow_unauthenticated_identities=False,
            cognito_identity_providers=[
                {
                    "clientId": user_pool_client.user_pool_client_id,
                    "providerName": user_pool.user_pool_provider_name,
                }
            ],
        )

        # API Gateway
        cors_options = apigw.CorsOptions(
            allow_origins=apigw.Cors.ALL_ORIGINS, allow_methods=apigw.Cors.ALL_METHODS
        )
        api = apigw.LambdaRestApi(
            self,
            "imageAPI",
            default_cors_preflight_options=cors_options,
            handler=serviceFn,
            proxy=False,
        )

        auth = apigw.CfnAuthorizer(
            self,
            "ApiGatewayAuthorizer",
            name="customer-authorizer",
            identity_source="method.request.header.Authorization",
            provider_arns=[user_pool.user_pool_arn],
            rest_api_id=api.rest_api_id,
            # type=apigw.AuthorizationType.COGNITO,
            type="COGNITO_USER_POOLS",
        )

        assumed_by = iam.FederatedPrincipal(
            "cognito-identity.amazon.com",
            conditions={
                "StringEquals": {"cognito-identity.amazonaws.com:aud": identity_pool.ref},
                "ForAnyValue:StringLike": {"cognito-identity.amazonaws.com:amr": "authenticated"},
            },
            assume_role_action="sts:AssumeRoleWithWebIdentity",
        )
        authenticated_role = iam.Role(
            self,
            "ImageRekognitionAuthenticatedRole",
            assumed_by=assumed_by,
        )
        # IAM policy granting users permission to get and put their pictures
        policy_statement = iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject"],
            effect=iam.Effect.ALLOW,
            resources=[
                image_bucket.bucket_arn + "/private/${cognito-identity.amazonaws.com:sub}/*",
                image_bucket.bucket_arn + "/private/${cognito-identity.amazonaws.com:sub}/",
                resized_image_bucket.bucket_arn
                + "/private/${cognito-identity.amazonaws.com:sub}/*",
                resized_image_bucket.bucket_arn + "/private/${cognito-identity.amazonaws.com:sub}/",
            ],
        )

        # IAM policy granting users permission to list their pictures
        list_policy_statement = iam.PolicyStatement(
            actions=["s3:ListBucket"],
            effect=iam.Effect.ALLOW,
            resources=[image_bucket.bucket_arn, resized_image_bucket.bucket_arn],
            conditions={
                "StringLike": {"s3:prefix": ["private/${cognito-identity.amazonaws.com:sub}/*"]}
            },
        )

        authenticated_role.add_to_policy(policy_statement)
        authenticated_role.add_to_policy(list_policy_statement)

        # Attach role to our Identity Pool
        cognito.CfnIdentityPoolRoleAttachment(
            self,
            "IdentityPoolRoleAttachment",
            identity_pool_id=identity_pool.ref,
            roles={"authenticated": authenticated_role.role_arn},
        )

        # Get some outputs from cognito
        cdk.CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        cdk.CfnOutput(self, "AppClientId", value=user_pool_client.user_pool_client_id)
        cdk.CfnOutput(self, "IdentityPoolId", value=identity_pool.ref)

        # New Amazon API Gateway with AWS Lambda Integration
        success_response = apigw.IntegrationResponse(
            status_code="200",
            response_parameters={"method.response.header.Access-Control-Allow-Origin": "'*'"},
        )
        error_response = apigw.IntegrationResponse(
            selection_pattern="(\n|.)+",
            status_code="500",
            response_parameters={"method.response.header.Access-Control-Allow-Origin": "'*'"},
        )

        request_template = json.dumps(
            {
                "action": "$util.escapeJavaScript($input.params('action'))",
                "key": "$util.escapeJavaScript($input.params('key'))",
            }
        )

        lambda_integration = apigw.LambdaIntegration(
            serviceFn,
            proxy=False,
            request_parameters={
                "integration.request.querystring.action": "method.request.querystring.action",
                "integration.request.querystring.key": "method.request.querystring.key",
            },
            request_templates={"application/json": request_template},
            passthrough_behavior=apigw.PassthroughBehavior.WHEN_NO_TEMPLATES,
            integration_responses=[success_response, error_response],
        )

        imageAPI = api.root.add_resource("images")

        success_resp = apigw.MethodResponse(
            status_code="200",
            response_parameters={"method.response.header.Access-Control-Allow-Origin": True},
        )
        error_resp = apigw.MethodResponse(
            status_code="500",
            response_parameters={"method.response.header.Access-Control-Allow-Origin": True},
        )

        # GET /images
        get_method = imageAPI.add_method(
            "GET",
            lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            request_parameters={
                "method.request.querystring.action": True,
                "method.request.querystring.key": True,
            },
            method_responses=[success_resp, error_resp],
        )
        # DELETE /images
        delete_method = imageAPI.add_method(
            "DELETE",
            lambda_integration,
            authorization_type=apigw.AuthorizationType.COGNITO,
            request_parameters={
                "method.request.querystring.action": True,
                "method.request.querystring.key": True,
            },
            method_responses=[success_resp, error_resp],
        )

        # Override the authorizer id because it doesn't work when defininting it as a param
        # in add_method
        get_method_resource = get_method.node.find_child("Resource")
        get_method_resource.add_property_override("AuthorizerId", auth.ref)
        delete_method_resource = delete_method.node.find_child("Resource")
        delete_method_resource.add_property_override("AuthorizerId", auth.ref)

        # Building SQS queue and DeadLetter Queue
        dl_queue = sqs.Queue(
            self,
            "ImageDLQueue",
            queue_name="ImageDLQueue",
        )

        dl_queue_opts = sqs.DeadLetterQueue(max_receive_count=2, queue=dl_queue)

        queue = sqs.Queue(
            self,
            "ImageQueue",
            queue_name="ImageQueue",
            visibility_timeout=cdk.Duration.seconds(30),
            receive_message_wait_time=cdk.Duration.seconds(20),
            dead_letter_queue=dl_queue_opts,
        )

        # S3 Bucket Create Notification to SQS
        # Whenever an image is uploaded add it to the queue

        image_bucket.add_object_created_notification(
            s3n.SqsDestination(queue), s3.NotificationKeyFilter(prefix="private/")
        )
