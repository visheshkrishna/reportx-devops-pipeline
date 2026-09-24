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

        stage('Release') {
      steps {
        sh '''
          set -e
          echo "--- Promoting build ${IMAGE_TAG} to production (frontend :3100, backend :8100) ---"

          docker tag ${BACKEND_IMG}:${IMAGE_TAG}  ${BACKEND_IMG}:release-${IMAGE_TAG}
          docker tag ${FRONTEND_IMG}:${IMAGE_TAG} ${FRONTEND_IMG}:release-${IMAGE_TAG}

          export POSTGRES_PORT=5434
          export BACKEND_PORT=8100
          export FRONTEND_PORT=3100
          export NEXT_PUBLIC_BACKEND_URL=http://localhost:8100
          export FRONTEND_URL=http://localhost:3100

          docker compose -p reportx-prod -f docker-compose.yml -f docker-compose.fullqa.yml down --remove-orphans || true
          docker compose -p reportx-prod -f docker-compose.yml -f docker-compose.fullqa.yml up -d --build

          echo "--- Verifying production health ---"
          for i in $(seq 1 60); do
            if docker compose -p reportx-prod -f docker-compose.yml -f docker-compose.fullqa.yml exec -T backend \
                 python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)" 2>/dev/null; then
              echo "Production healthy after ${i}s"
              break
            fi
            if [ "$i" = "60" ]; then echo "Production never became healthy — staging remains live"; exit 1; fi
            sleep 1
          done

          docker compose -p reportx-prod -f docker-compose.yml -f docker-compose.fullqa.yml ps
          echo "Released ${IMAGE_TAG} to production: http://localhost:3100"
        '''
      }
    }

        stage('Monitoring') {
      steps {
        sh '''
          set -e
          echo "--- Starting monitoring stack (Prometheus :9090, Grafana :3001) ---"
          docker compose -p reportx-monitoring -f monitoring/docker-compose.monitoring.yml up -d

          echo "--- Waiting for Prometheus ---"
          for i in $(seq 1 60); do
            if docker compose -p reportx-monitoring -f monitoring/docker-compose.monitoring.yml \
                 exec -T prometheus wget -q -O- http://localhost:9090/-/ready >/dev/null 2>&1; then
              echo "Prometheus ready after ${i}s"
              break
            fi
            if [ "$i" = "60" ]; then echo "Prometheus never became ready"; exit 1; fi
            sleep 1
          done

          echo "--- Alert rules loaded ---"
          docker compose -p reportx-monitoring -f monitoring/docker-compose.monitoring.yml \
            exec -T prometheus wget -q -O- http://localhost:9090/api/v1/rules \
            | tr ',' '\\n' | grep '"name"' || true

          echo "--- Waiting for first scrape ---"
          sleep 15

          echo "--- Current probe results (1 = up, 0 = down) ---"
          docker compose -p reportx-monitoring -f monitoring/docker-compose.monitoring.yml \
            exec -T prometheus wget -q -O- 'http://localhost:9090/api/v1/query?query=probe_success' \
            | tr '{' '\\n' | grep -E 'env|value' || true

          echo "Prometheus: http://localhost:9090/alerts"
          echo "Grafana:    http://localhost:3001 (admin/admin)"
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