---
auto_execution_mode: 0
description: Review code changes for bugs, security issues, and improvements
---
# Code Review Workflow

## Global Rules for VideoProcessor Project

### 🔄 **Auto-Push Rules**
- **Always push** after any meaningful code changes
- **Commit format**: Use conventional commits (feat:, fix:, docs:, etc.)
- **Auto-sync**: Push to origin immediately after commit
- **Branch protection**: Main branch requires clean status

### 📋 **Review Checklist**
1. **Functionality**: Does the code work as expected?
2. **Security**: Are credentials and sensitive data protected?
3. **Performance**: Is FFmpeg processing efficient?
4. **Error Handling**: Are edge cases properly handled?
5. **Documentation**: Is README.md up to date?

### 🚫 **Never Commit**
- `.env` files (contains secrets)
- `auth.json` (OAuth credentials)
- `token.json` (OAuth tokens)
- `credentials.json` (service account keys)
- Log files
- Temporary files

### ✅ **Always Include**
- Code changes with proper commit messages
- Updated documentation
- Test results (if applicable)

### 🎯 **Project-Specific Rules**
- FFmpeg commands must be tested before pushing
- OAuth authentication must be working
- Assets folder paths must be correct
- GitHub Actions workflow must be valid

## Review Process

1. **Analyze changes** for bugs and improvements
2. **Check security** of credential handling
3. **Verify functionality** with test runs
4. **Update documentation** if needed
5. **Push changes** with proper commit message

This workflow ensures code quality and maintains project stability.