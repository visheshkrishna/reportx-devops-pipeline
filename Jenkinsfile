pipeline {
  agent any

  options {
    timestamps()
    buildDiscarder(logRotator(numToKeepStr: '15'))
    timeout(time: 30, unit: 'MINUTES')
  }

  environment {
    DOCKER_BUILDKIT = "1"
    IMAGE_TAG    = "${env.BUILD_NUMBER}"
    BACKEND_IMG  = "reportx-backend"
    FRONTEND_IMG = "reportx-frontend"
  }

  stages {

    stage('Build') {
      steps {
        sh '''
          set -e
          echo "--- Building production images, tag ${IMAGE_TAG} ---"

          docker build --target base \
            -t ${BACKEND_IMG}:${IMAGE_TAG} \
            -t ${BACKEND_IMG}:latest ./backend

          docker build --target runner \
            --build-arg NEXT_PUBLIC_BACKEND_URL=http://localhost:8000 \
            -t ${FRONTEND_IMG}:${IMAGE_TAG} \
            -t ${FRONTEND_IMG}:latest ./frontend

          docker image inspect ${BACKEND_IMG}:${IMAGE_TAG} --format 'backend artefact: {{.Id}}'
          docker image inspect ${FRONTEND_IMG}:${IMAGE_TAG} --format 'frontend artefact: {{.Id}}'
        '''
      }
    }

    stage('Test') {
      steps {
        sh '''
          set -e
          echo "--- Backend: pytest (141 tests) ---"
          docker build --target test -t ${BACKEND_IMG}-test:${IMAGE_TAG} ./backend
          docker run --rm ${BACKEND_IMG}-test:${IMAGE_TAG} pytest -q

          echo "--- Frontend: vitest (203 tests) ---"
          docker build --target test -t ${FRONTEND_IMG}-test:${IMAGE_TAG} ./frontend
          docker run --rm ${FRONTEND_IMG}-test:${IMAGE_TAG} npm test
        '''
      }
    }

    stage('Code Quality') {
      steps {
        sh '''
          set -e
          echo "--- Backend: ruff + black ---"
          docker run --rm ${BACKEND_IMG}-test:${IMAGE_TAG} ruff check .
          docker run --rm ${BACKEND_IMG}-test:${IMAGE_TAG} black --check .

          echo "--- Frontend: eslint + tsc ---"
          docker run --rm ${FRONTEND_IMG}-test:${IMAGE_TAG} npm run lint
          docker run --rm ${FRONTEND_IMG}-test:${IMAGE_TAG} npm run typecheck
        '''
      }
    }
  }

  post {
    success { echo "Pipeline passed. Artefacts tagged ${IMAGE_TAG}." }
    failure { echo "Pipeline failed at stage: ${env.STAGE_NAME}" }
    always  { sh 'docker image prune -f --filter "until=24h" || true' }
  }
}
