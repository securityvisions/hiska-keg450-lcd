PYTHON ?= python3
PREFIX ?= /usr/local
RULE   := udev/99-hiska-keg450.rules

.PHONY: help check info testcard install-udev install-service uninstall-service service

help:
	@echo "make info        - show the detected hidraw nodes"
	@echo "make testcard    - upload the 4-colour test card"
	@echo "make check       - byte-compile and smoke-test the scripts"
	@echo "make install-udev- install the udev rule (needs sudo)"
	@echo "make install-service  - install and start the live clock user unit"
	@echo "make uninstall-service - stop, disable and remove it"

info:
	./keg450.py info

testcard:
	./keg450.py testcard

check:
	$(PYTHON) -m py_compile keg450.py live.py
	./keg450.py --version
	./keg450.py light --mode list
	./keg450.py settings --dry-run
	./live.py --help > /dev/null

install-udev:
	sudo install -m644 $(RULE) /etc/udev/rules.d/
	sudo udevadm control --reload-rules
	sudo udevadm trigger --subsystem-match=hidraw

install-service:
	./live.py --install-service

uninstall-service:
	./live.py --uninstall-service

# kept for older notes and README links: now identical to install-service
service: install-service
