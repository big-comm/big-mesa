/*
 * check() of lib32-mesa-big: what a 32-bit game does first.
 *
 *   test-lib32 gl      OpenGL through EGL (surfaceless, no display), prints
 *                      GL_RENDERER and GL_VERSION
 *   test-lib32 vulkan  Vulkan, prints the first device's name and driverInfo
 *
 * Built with gcc -m32 against the 32-bit libglvnd and Vulkan loader, so it
 * loads the 32-bit drivers the environment points at.
 */
#include <stdio.h>
#include <string.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <vulkan/vulkan.h>

static int test_gl(void)
{
	PFNEGLGETPLATFORMDISPLAYEXTPROC get_display =
		(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
	if (!get_display) {
		fprintf(stderr, "no eglGetPlatformDisplayEXT\n");
		return 1;
	}
	EGLDisplay dpy = get_display(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, NULL);
	EGLint major, minor;
	if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, &major, &minor)) {
		fprintf(stderr, "eglInitialize failed: 0x%x\n", eglGetError());
		return 1;
	}
	eglBindAPI(EGL_OPENGL_API);
	EGLContext ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, NULL);
	if (ctx == EGL_NO_CONTEXT || !eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) {
		fprintf(stderr, "no OpenGL context: 0x%x\n", eglGetError());
		return 1;
	}
	printf("GL_RENDERER: %s\n", (const char *)glGetString(GL_RENDERER));
	printf("GL_VERSION: %s\n", (const char *)glGetString(GL_VERSION));
	eglTerminate(dpy);
	return 0;
}

static int test_vulkan(void)
{
	VkApplicationInfo app = {
		.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
		.apiVersion = VK_API_VERSION_1_2,
	};
	VkInstanceCreateInfo info = {
		.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
		.pApplicationInfo = &app,
	};
	VkInstance instance;
	VkResult result = vkCreateInstance(&info, NULL, &instance);
	if (result != VK_SUCCESS) {
		fprintf(stderr, "vkCreateInstance failed: %d\n", result);
		return 1;
	}
	uint32_t count = 1;
	VkPhysicalDevice device;
	result = vkEnumeratePhysicalDevices(instance, &count, &device);
	if ((result != VK_SUCCESS && result != VK_INCOMPLETE) || count == 0) {
		fprintf(stderr, "no Vulkan device: %d\n", result);
		return 1;
	}
	VkPhysicalDeviceDriverProperties driver = {
		.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES,
	};
	VkPhysicalDeviceProperties2 props = {
		.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,
		.pNext = &driver,
	};
	vkGetPhysicalDeviceProperties2(device, &props);
	printf("deviceName: %s\n", props.properties.deviceName);
	printf("driverInfo: %s\n", driver.driverInfo);
	vkDestroyInstance(instance, NULL);
	return 0;
}

int main(int argc, char **argv)
{
	if (argc == 2 && !strcmp(argv[1], "gl"))
		return test_gl();
	if (argc == 2 && !strcmp(argv[1], "vulkan"))
		return test_vulkan();
	fprintf(stderr, "usage: %s gl|vulkan\n", argv[0]);
	return 2;
}
