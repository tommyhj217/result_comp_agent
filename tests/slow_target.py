"""격리 실행 시간 초과 테스트용 느린 비교 대상."""
import time


def compare_slow(**kwargs):
    time.sleep(30)


def compare_crash(**kwargs):
    import os

    os._exit(3)
