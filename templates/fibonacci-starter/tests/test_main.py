"""Tests for main module."""

from src.main import fibonacci


def test_fibonacci_base_cases():
    """Test Fibonacci base cases."""
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1


def test_fibonacci_sequence():
    """Test Fibonacci sequence values."""
    assert fibonacci(2) == 1
    assert fibonacci(3) == 2
    assert fibonacci(4) == 3
    assert fibonacci(5) == 5
    assert fibonacci(6) == 8
    assert fibonacci(7) == 13


def test_fibonacci_larger_values():
    """Test larger Fibonacci values."""
    assert fibonacci(10) == 55
    assert fibonacci(15) == 610
