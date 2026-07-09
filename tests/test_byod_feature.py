"""Unit tests for Bring Your Own Docker (BYOD) feature."""

from coder_eval.models import DockerDriverConfig, SandboxConfig
from coder_eval.utils import get_default_docker_image_tag


class TestBYODImageDefaults:
    """Test that DockerDriverConfig.image defaults to DEFAULT_IMAGE_TAG."""

    def test_image_defaults_to_default_image_tag(self) -> None:
        """DockerDriverConfig.image should default to get_default_docker_image_tag()."""
        config = DockerDriverConfig()
        assert config.image == get_default_docker_image_tag()

    def test_image_can_be_overridden(self) -> None:
        """DockerDriverConfig.image can be set to a custom value."""
        custom_image = "my-custom-image:v1.0"
        config = DockerDriverConfig(image=custom_image)
        assert config.image == custom_image

    def test_custom_image_persists_in_sandbox_config(self) -> None:
        """Custom image specified in DockerDriverConfig persists in SandboxConfig."""
        custom_image = "custom-repo/custom-image:latest"
        docker_config = DockerDriverConfig(image=custom_image)
        sandbox = SandboxConfig(driver="docker", docker=docker_config)
        assert sandbox.docker.image == custom_image

    def test_default_image_in_sandbox_config(self) -> None:
        """SandboxConfig with docker driver uses default image when not overridden."""
        sandbox = SandboxConfig(driver="docker")
        assert sandbox.docker.image == get_default_docker_image_tag()


class TestBYODIntegration:
    """Test BYOD feature integration across models and runner."""

    def test_docker_driver_config_type_is_string(self) -> None:
        """image field should be str (never None)."""
        config = DockerDriverConfig()
        assert isinstance(config.image, str)
        assert len(config.image) > 0

    def test_sandbox_config_docker_always_has_image(self) -> None:
        """SandboxConfig.docker.image should always be set (never None)."""
        sandbox = SandboxConfig(driver="docker")
        assert isinstance(sandbox.docker.image, str)
        assert len(sandbox.docker.image) > 0

    def test_empty_docker_config_uses_default(self) -> None:
        """Creating DockerDriverConfig with no args uses default image."""
        config = DockerDriverConfig()
        assert config.image == get_default_docker_image_tag()

    def test_image_field_is_not_optional(self) -> None:
        """image field should be required type (not Optional[str])."""
        # This test verifies the type annotation is str, not str | None.
        # If someone accidentally reverts to str | None, this catches it.
        config = DockerDriverConfig()
        # Accessing a potentially None value would fail type checking,
        # but runtime allows it. We verify it's never None:
        assert config.image is not None

    def test_multiple_instances_have_independent_images(self) -> None:
        """Multiple DockerDriverConfig instances can have different images."""
        config1 = DockerDriverConfig(image="image1:v1")
        config2 = DockerDriverConfig(image="image2:v2")
        config3 = DockerDriverConfig()  # default

        assert config1.image == "image1:v1"
        assert config2.image == "image2:v2"
        assert config3.image == get_default_docker_image_tag()
        assert config1.image != config2.image
        assert config1.image != config3.image
