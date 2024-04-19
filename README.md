# Caddy Chatbot

This repository stores the project for the LLM enabled advisor support bot. This contains the logic for the chatbot and responses, and also relies on storage of the data using [caddy_scraper](https://github.com/i-dot-ai/caddy_scraper).

## How to Run

This version is intended for deployment on serverless infrastructure, and will ideally use Docker and AWS Sam.

### Environment Management
We recommend using [Poetry](https://github.com/python-poetry/poetry) for managing dependencies.

To create your virtual environment, run

```bash
$ 	poetry install
$ 	poetry shell
```

### With AWS SAM CLI

Local development is easier running AWS SAM, either [installed directly](https://aws.amazon.com/serverless/aws-sam/) or through [pip](https://pypi.org/project/aws-sam-cli/).

You will also require AWS CLI, either [installed directly](https://aws.amazon.com/cli/) or through [pip](https://github.com/aws/aws-cli).

To confirm install, run

```bash
$ aws --version
```

To configure, run

```bash
$ aws configure
```

You will need to create both an .env and env.json following the examples given with your Anthropic key.

To build your Lambda function, run

```bash
$ make build-lambda
```

You must be running docker to execute this.  Once your docker image has been built, you can test your lambda with a sample event by running

```bash
$ make test-chat-lambda
```

### Use vscode on github (codespace)

To develop in codespaces, ensure you define your environment variables through Github settings.

[![Open in Remote - Containers](https://img.shields.io/static/v1?label=Remote%20-%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://codespaces.new/PMO-Data-Science/10ds-advice-bot?quickstart=1)

If you are using windows or do not want to install vscode on your machine, you can click on the badge above to spin up a codespace environment.

## Developing with Local DynamoDB

To explore the connection to DynamoDB, I have attached the docker-compose file to spin up a local DynamoDB.  This will have to be span up before using the relevant notebook.

```bash
$ docker-compose up
```

## Troubleshooting
If SAM CLI is not finding docker, try the following:

Show the docker context information:
```bash
$ docker context ls
```
Provide the `DOCKER_HOST` environment variable when you run SAM CLI commands by putting the following at the start of your commands.
Replace the host endpoint with what you see from `docker context ls`.
```bash
$ DOCKER_HOST=unix:///Users/user_name/.docker/run/docker.sock
```
Further details [here](https://github.com/aws/aws-sam-cli/issues/4329#issuecomment-1289588827).

## Testing

Running tests on platform agnostic Caddy components with pytest. Tests are stored in tests/caddy_components, and can be invoked by running either of the below:

This automatically starts the sam local endpoint and runs pytest -v
```bash
$ make run-tests
```

For a manual approach run the below and then once running open a new terminal and run pytest
```bash
$ sam local start-lambda
```
```bash
$ pytest
```

## Deployment

We'll use AWS SAM to create and deploy all the relevant resources.  You will require your AWS CLI configured with the correct permissions.

```bash
$ sam build -t template.yaml --use-container
```
Once the build is complete, you can deploy the stack with

```bash
$ sam deploy --guided --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND CAPABILITY_NAMED_IAM
```

For ease of deletion, you can remove all the created resources with

```bash
$ aws cloudformation delete-stack --stack-name caddyStack
```