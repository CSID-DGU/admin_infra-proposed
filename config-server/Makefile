RELEASE ?= config-server
NAMESPACE ?= ailab-infra
# 기존 Deployment의 immutable selector를 유지하는 안전한 기본값. 새 릴리스는
# `make deploy SELECTOR_MODE=release ...`로 release별 selector를 사용한다.
SELECTOR_MODE ?= legacy

deploy:
	@test -n "$(TAG)" || (echo "TAG is required" >&2; exit 2)
	@test -n "$(VALUES)" || (echo "VALUES is required (use a reviewed FARM/LAB values file)" >&2; exit 2)
	helm upgrade --install $(RELEASE) ./Chart -n $(NAMESPACE) --create-namespace -f $(VALUES) \
		--set config.namespace=$(NAMESPACE) \
		--set compatibility.deploymentSelectorMode=$(SELECTOR_MODE) \
		--set image.tag=$(TAG)
