---
auto_execution_mode: 1
description: Automatically push changes after code modifications
---
# Auto-Push Workflow

## 🔄 **Automatic Push Rules**

### **Trigger Conditions**
- Any code file is modified (*.py, *.yml, *.md)
- Configuration files are updated (.env.example, requirements.txt)
- Documentation changes are made

### **Auto-Execution Steps**
1. **Stage changes**: `git add .`
2. **Commit with conventional format**:
   - `feat:` for new features
   - `fix:` for bug fixes
   - `docs:` for documentation
   - `refactor:` for code refactoring
   - `test:` for test changes
3. **Push to origin**: `git push origin main`

### **Commit Message Format**
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### **Examples**
- `feat(endscreen): add auto-duration detection`
- `fix(oauth): resolve token refresh issue`
- `docs(readme): update setup instructions`
- `refactor(ffmpeg): simplify video processing pipeline`

### **Safety Checks**
- Never commit sensitive files (.env, auth.json, token.json)
- Verify code functionality before pushing
- Ensure all tests pass (if applicable)

### **Global Sync Rules**
- **Always push** after meaningful changes
- **Keep remote updated** with local modifications
- **Maintain clean commit history**
- **Follow conventional commit standards**

This workflow ensures that all improvements are immediately synchronized with the remote repository, maintaining code consistency and enabling continuous deployment.
