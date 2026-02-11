<p align="center">
  <img src="FabricOps_250.png" alt="FabricOps Logo">
</p>

## FabricOps - The Microsoft Fabric DevOps Automation Platform

A comprehensive Microsoft Fabric automation platform demonstrating enterprise-grade DevOps practices, infrastructure as code (IaC), CI/CD workflows, and modern ways-of-working for data platform delivery.

## Overview

FabricOps is a production-ready automation framework designed to showcase best practices for Microsoft Fabric workspace management, solution delivery, and collaborative development workflows. This platform serves as both a demonstration tool for community conferences and events, and a boilerplate reference for enterprise Fabric implementations.

The solution represents a realistic enterprise scenario with:
- **Multi-environment architecture** (dev, test, prod)
- **Automated infrastructure provisioning and teardown**
- **Feature-based development workflows**
- **Comprehensive CI/CD pipelines**
- **Quality gates and validation processes**

It includes fully automated pipelines that support modern ways of working, validation, and deployment across environments while maintaining workspace synchronization and orchestrating complex cross-workspace deployments.

## Key Capabilities Demonstrated

### Infrastructure as Code (IaC)
- **Automated Fabric workspace setup and teardown**
- **Dynamic environment provisioning**
- **Capacity management and optimization**
- **Resource lifecycle automation**

### CI/CD & Deployment Strategies
- **Multi-stage release pipelines** (dev → test → prod)
- **Single-stage deployment** for targeted releases
- **Octopus deployment patterns** for selective feature promotion
- **Feature workspace automation** for isolated development

### Quality & Validation
- **Best Practice Analyzer (BPA) rule validation**
- **Automated semantic model testing**
- **Pull request validation workflows**
- **Quality gates and approval processes**

### Collaboration & Ways-of-Working
- **Feature branch automation** with workspace provisioning
- **Automated workspace synchronization**
- **Cross-workspace reference management**
- **Git integration with workspace source control**

## Tooling and Technologies

### Core Automation Stack
- **Python automation framework** - Custom scripts for Fabric operations and orchestration
- **fabric-cicd library** - Primary deployment and management tool for Fabric items
- **Fabric CLI integration** - Native Fabric command-line operations
- **Azure DevOps / GitHub** - CI/CD pipeline orchestration and source control

### Validation & Quality
- **Tabular Editor** 
  - BPA rule validation during build processes
  - TMDL format conversion and management
  - Semantic model optimization and testing
- **Custom BPA Rules** - Enterprise-grade validation rules for semantic models

### Authentication & Security
- **Service Principal authentication** - Secure API access patterns
- **Environment-specific credentials** - Isolated security contexts
- **Token management** - Automated credential refresh and rotation

## Prerequisites

To use FabricOps in your environment, you'll need:

### Microsoft Fabric Requirements
- **Microsoft Fabric capacity** (trial, F64, or higher paid SKU)
- **Fabric workspace administrator permissions**
- **Power BI Premium or Fabric capacity allocation**

### Development Platform
- **Azure DevOps** or **GitHub** account with permissions to create:
  - Projects and repositories
  - Service connections and secrets
  - Pipeline definitions and workflows

### Service Principal Setup
- **Azure AD/Entra ID Service Principal** with:
  - Access to Fabric REST APIs ([configuration guide](https://learn.microsoft.com/en-us/fabric/admin/service-admin-portal-developer))
  - Appropriate Fabric workspace permissions
  - Project Administrator role (Azure DevOps) or equivalent GitHub permissions

### Fabric CLI Version Requirement
**Fabric CLI version 1.0.0+ is required**

Version 1.0.0 introduced the `-f`/`--force` flag in the `get` command to suppress warnings on sensitivity labels. The release notes state:
> "Added a confirmation prompt in get to acknowledge that exported items do not include sensitivity labels; use -f to skip."

Versions <1.0.0 do not support the `-f` switch and will cause automation scripts to fail.

## Project Structure

```
├── .azure-pipelines/             # Azure DevOps pipeline definitions
├── .github/                      # GitHub Actions workflows and templates
├── automation/                   # Automation scripts and configuration
│   ├── credentials/              # Credential templates and configuration
│   ├── resources/               # Environment definitions and parameters
│   │   ├── BPARules.json        # Custom Best Practice Analyzer rules
│   │   ├── environments/        # Environment-specific configurations
│   │   └── parameters/          # Deployment parameters and bindings
│   └── scripts/                 # Core automation scripts
│       ├── fabric_setup.py     # Infrastructure setup/teardown
│       ├── fabric_release.py   # Solution deployment
│       ├── fabric_feature_*    # Feature management automation
│       ├── locale/             # Local development utilities
│       └── modules/            # Reusable automation modules
└── solution/                    # Fabric workspace items
    ├── core/                    # Core infrastructure components
    ├── ingest/                   # Data ingestion pipelines
    ├── store/                    # Data storage (lakehouses)
    ├── prepare/                  # Data preparation and transformation
    ├── orchestrate/              # Orchestration pipelines
    ├── model/                    # Semantic models (TMDL)
    └── present/                  # Presentation layer (reports)
```

## Getting Started

### Initial Setup

1. **Create your repository**
   ```bash
   # Create new Azure DevOps project or GitHub repository
   # Clone this repository as a template
   git clone https://github.com/gronnerup/FabricOps.git
   cd FabricOps
   ```

2. **Configure authentication**
   ```bash
   # Copy credential template and configure
   cp automation/credentials/credentials_template.json automation/credentials/credentials.json
   # Update with your Service Principal details
   ```

3. **Update environment configurations**
   - Modify `automation/resources/environments/infrastructure.json`
   - Configure environment-specific files (`infrastructure.dev.json`, etc.)
   - Update Git provider settings in environment files

4. **Run platform setup**
   ```bash
   # For Azure DevOps
   python automation/scripts/locale/locale_setup_azuredevops.py
   
   # For GitHub
   python automation/scripts/locale/locale_setup_github.py
   ```

5. **Deploy infrastructure**
   - Commit changes and create pull request
   - Run "Solution IaC – Setup" pipeline to provision Fabric infrastructure

### Feature Development Workflow

FabricOps supports automated feature development with dedicated workspaces:

1. **Create feature branch**
   ```bash
   # Feature branch naming triggers workspace creation
   git checkout -b "feature/orchestrate/new_pipeline"
   ```

2. **Automatic workspace provisioning**
   - Feature workspaces are automatically created
   - Only relevant layers are provisioned based on branch name
   - Isolated development environment ready for use

3. **Development and testing**
   - Work in isolated feature workspace
   - Automatic workspace synchronization
   - Local testing and validation

4. **Integration and deployment**
   - Create pull request with BPA validation
   - Automated workspace cleanup after merge
   - Promotion through deployment pipeline

### Deployment Options

FabricOps provides multiple deployment strategies:

#### Multi-Stage Deployment
- **Automated progression** through environments
- **Quality gates** between stages

#### Single-Stage Deployment
- **Targeted environment deployment**
- **Runtime environment selection**
- **Quick hotfix capabilities**

#### Octopus Deployment
- **Selective feature promotion**
- **Branch-based deployment decisions**
- **Advanced release orchestration**

## Optional: Build Validation and Branch Policies

It is strongly recommended to protect the `main` branch using branch policies.

### Recommended Settings
- Minimum number of required approvers
- Optional requirement for linked work items

### BPA Validation Setup

**Azure DevOps:**
1. Navigate to Branches
2. Select Branch Policies  
3. Under Build Validation:
   - Add pipeline: PR – BPA Validation
   - Trigger: Automatic
   - Policy requirement: Required
   - Expiration: 12 hours

If BPA validation fails with severity 3 violations, the pull request will be blocked.

**GitHub:**
For GitHub, BPA validation is already configured using a `pull_request` trigger targeting `main`. The configuration is defined in `pr-validation.yaml`.

**Note:** On some systems (especially Windows), cloning or working with this repository may fail due to long file paths. If you encounter path length issues, run the following command before cloning:
```bash
git config --global core.longpaths true
```

## Advanced Features

### Workspace Orchestration
- **Cross-workspace dependency management**
- **Automatic reference replacement** during deployment
- **Environment-specific workspace binding**
- **Intelligent workspace provisioning**

### Connection Management
- **Dynamic connection string generation**
- **Environment-specific binding**
- **Secure credential management**
- **Automated connection updates**

### Monitoring and Observability
- **Deployment tracking and logging**
- **Performance monitoring**
- **Error handling and recovery**
- **Audit trails and compliance**

## Additional Resources

### Conference Materials and Presentations
For related slide decks, presentations, and session materials on Microsoft Fabric, Power BI, Automation, CI/CD, and DevOps best practices, visit:

**[Session Archive Repository](https://github.com/gronnerup/SessionArchive)**

This repository contains presentation materials, demo scripts, and additional resources from various community conferences and events covering topics such as:
- Microsoft Fabric automation strategies
- CI/CD implementation patterns
- DevOps best practices for data platforms
- Modern ways-of-working for analytics teams

## Disclaimer

**Use of this code is entirely at your own risk.**

- This solution is provided as inspiration and a boilerplate reference
- No guarantees are provided regarding correctness or future compatibility
- Extensive testing in your environment is recommended before production use
- Regular updates may be required to maintain compatibility with Fabric platform changes

## Contributing

FabricOps is continuously evolving to demonstrate the latest Microsoft Fabric capabilities and DevOps best practices. Contributions, feedback, and suggestions are welcome for improving the demonstration scenarios and automation capabilities.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

The demonstration datasets and examples are intended for learning and development purposes only. Please ensure compliance with your organization's data governance and security policies when adapting this framework for production use. 