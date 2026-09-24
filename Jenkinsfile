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

    stage('Security') {
      steps {
        sh '''
          set -e
          echo "--- Trivy: backend image ---"
          docker run --rm \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v trivy-cache:/root/.cache/ \
            aquasec/trivy:latest image --quiet \
            --severity HIGH,CRITICAL --scanners vuln --exit-code 0 \
            ${BACKEND_IMG}:${IMAGE_TAG}

          echo "--- Trivy: frontend image ---"
          docker run --rm \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v trivy-cache:/root/.cache/ \
            aquasec/trivy:latest image --quiet \
            --severity HIGH,CRITICAL --scanners vuln --exit-code 0 \
            ${FRONTEND_IMG}:${IMAGE_TAG}

          echo "--- npm audit: frontend dependencies ---"
          docker run --rm ${FRONTEND_IMG}-test:${IMAGE_TAG} npm audit --audit-level=high || true
        '''
      }
    }

    stage('Deploy') {
      steps {
        sh '''
          set -e
          echo "--- Deploying to staging (frontend :3000, backend :8000) ---"

          docker compose -p reportx-staging down --remove-orphans || true
          docker compose -p reportx-staging up -d --build

          echo "--- Waiting for backend health ---"
          for i in $(seq 1 60); do
            if docker compose -p reportx-staging exec -T backend \
                 python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)" 2>/dev/null; then
              echo "Backend healthy after ${i}s"
              break
            fi
            if [ "$i" = "60" ]; then echo "Backend never became healthy"; exit 1; fi
            sleep 1
          done

          docker compose -p reportx-staging ps
          echo "Staging deployed: http://localhost:3000"
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